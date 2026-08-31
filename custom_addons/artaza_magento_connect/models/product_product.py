import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Global/default scope: applies to every website unless overridden per-website.
MAGENTO_DEFAULT_STORE_ID = 0


class ProductProduct(models.Model):
    _inherit = 'product.product'

    magento_stock_dirty = fields.Boolean(
        string="Stock pending sync to Magento",
        default=False, index=True, copy=False,
        help="Set when stock changes; the cron pushes it and clears it.",
    )
    magento_price_dirty = fields.Boolean(
        string="Price pending sync to Magento",
        default=False, index=True, copy=False,
        help="Set when the sales price changes; the cron pushes it.",
    )
    magento_tax_dirty = fields.Boolean(
        string="Tax pending sync to Magento",
        default=False, index=True, copy=False,
        help="Set when the customer tax changes; the cron pushes the matching "
             "Magento tax class mapped to the product's sale tax.",
    )

    # ── On-hand per warehouse ──────────────────────────────────
    @api.model
    def _magento_qty_by_warehouse(self, products, warehouses):
        """{product_id: {warehouse_code: on_hand}} reading stock.quant.

        Discriminates per warehouse by summing each warehouse's internal
        locations (stock location + children). It does not use the `warehouse`
        context of `qty_available` because that does not split per warehouse.
        """
        Location = self.env['stock.location']
        wh_location_ids = {}
        for wh in warehouses:
            locations = Location.search([
                ('id', 'child_of', wh.view_location_id.id),
                ('usage', '=', 'internal'),
            ])
            wh_location_ids[wh.code] = set(locations.ids)

        all_location_ids = set().union(*wh_location_ids.values()) if wh_location_ids else set()

        qty_per_loc = {}
        if products and all_location_ids:
            groups = self.env['stock.quant'].read_group(
                [('product_id', 'in', products.ids),
                 ('location_id', 'in', list(all_location_ids))],
                ['quantity:sum'],
                ['product_id', 'location_id'],
                lazy=False,
            )
            for group in groups:
                product_id = group['product_id'][0]
                location_id = group['location_id'][0]
                qty_per_loc[product_id, location_id] = group['quantity']

        result = {}
        for product in products:
            result[product.id] = {
                wh.code: sum(
                    qty_per_loc.get((product.id, loc_id), 0.0)
                    for loc_id in wh_location_ids[wh.code]
                )
                for wh in warehouses
            }
        return result

    # ── Module grid ────────────────────────────────────────────
    @api.model
    def magento_stock_matrix(self, search=None, offset=0, limit=50):
        """Paginated per-warehouse stock matrix for the single-product sync."""
        warehouses = self.env['stock.warehouse'].search([], order='id')

        domain = [('is_storable', '=', True), ('default_code', '!=', False)]
        if search:
            domain += ['|', ('name', 'ilike', search), ('default_code', 'ilike', search)]

        total = self.search_count(domain)
        products = self.search(domain, order='default_code', offset=offset, limit=limit)

        qty_by_wh = self._magento_qty_by_warehouse(products, warehouses)

        rows = []
        for product in products:
            qtys = qty_by_wh.get(product.id, {})
            rows.append({
                'id': product.id,
                'sku': product.default_code,
                'name': product.name,
                'qtys': qtys,
                'total': sum(qtys.values()),
            })

        return {
            'warehouses': [{'code': wh.code, 'name': wh.name} for wh in warehouses],
            'rows': rows,
            'total': total,
            'offset': offset,
            'limit': limit,
        }

    # ── Push helpers (used by the button and the cron) ─────────
    # These talk to Magento directly. The translation between the two systems
    # (warehouse → source, tax → tax class) happens here, against the mappings
    # configured in Settings ▸ Magento Connect.

    @api.model
    def _magento_push_stock(self, products):
        """Push the per-warehouse stock of `products` straight to Magento (MSI).

        Warehouses pointing at the **same** Magento source are summed: MSI holds
        one quantity per (sku, source), so two Odoo warehouses feeding one source
        must arrive added up, not overwriting each other. A warehouse with no
        source mapped is **skipped and reported** — never guessed.
        """
        warehouses = self.env['stock.warehouse'].search([], order='id')
        qty_by_wh = self._magento_qty_by_warehouse(products, warehouses)

        aggregated = {}
        skipped = []
        for product in products:
            for warehouse in warehouses:
                qty = qty_by_wh[product.id].get(warehouse.code, 0.0)
                source = warehouse.magento_source_id
                if not source:
                    skipped.append({
                        'sku': product.default_code,
                        'warehouse_code': warehouse.code,
                    })
                    continue
                key = (product.default_code, source.code)
                aggregated[key] = aggregated.get(key, 0.0) + qty

        source_items = [{
            'sku': sku,
            'source_code': source_code,
            'quantity': qty,
            'status': 1 if qty > 0 else 0,
        } for (sku, source_code), qty in aggregated.items()]

        if source_items:
            self.env['artaza.magento.client'].write_source_items(source_items)

        # A warehouse with no source is one CONFIG gap, not N product failures:
        # report it once per warehouse so the history stays readable.
        failures = [{
            'ref': code,
            'reason': self.env._(
                "Warehouse %s has no Magento source mapped, so its stock is not "
                "being sent. Map it in Settings ▸ Magento Connect ▸ Mapping.", code),
        } for code in sorted({item['warehouse_code'] for item in skipped})]

        # The stock-matrix front reads `skipped` to warn about pending warehouses.
        return {
            'written': len(source_items),
            'skus': len({item['sku'] for item in source_items}),
            'skipped': skipped,
            'ok_skus': sorted({item['sku'] for item in source_items}),
            'failures': failures,
        }

    @api.model
    def _magento_push_price(self, products):
        """Push the base price (list_price, IVA included) straight to Magento.

        Written at the **global scope** (`store_id` 0): Magento runs
        `Catalog Prices = Including Tax`, so the value goes as-is and Magento
        back-calculates the tax. Catalog price rules apply on top — untouched.
        """
        prices = [{
            'sku': product.default_code,
            'price': product.list_price,
            'store_id': MAGENTO_DEFAULT_STORE_ID,
        } for product in products]
        failed = self.env['artaza.magento.client'].write_base_prices(prices)
        # Magento answers 200 with a per-item error array, so a partial failure
        # would otherwise read as a clean run.
        failed_skus = {str(item.get('sku')) for item in failed if isinstance(item, dict)}
        return {
            'written': len(prices) - len(failed),
            'ok_skus': [p['sku'] for p in prices if p['sku'] not in failed_skus],
            'failures': [{
                'ref': str(item.get('sku')) if isinstance(item, dict) else str(item),
                'reason': str(item),
            } for item in failed],
        }

    def _magento_sale_tax(self):
        """The customer (sale) tax this product carries, or an empty recordset.

        Magento holds one tax class per product, so only the first sale tax is
        mirrored; a product with several is logged and the first one wins.
        """
        self.ensure_one()
        taxes = self.taxes_id.filtered(lambda t: t.type_tax_use in ('sale', 'all'))
        if len(taxes) > 1:
            _logger.warning(
                "Product %s has %s sale taxes; syncing the first one (%s) to Magento.",
                self.default_code, len(taxes), taxes[0].name,
            )
        return taxes[:1]

    @api.model
    def _magento_push_tax(self, products):
        """Push each product's Magento tax class, resolved from its sale tax.

        Grouped **by class**, so it is one request per tax class (there are a
        handful) instead of one per product. A tax with no class mapped, or a
        product with no sale tax, is skipped and reported — the wrong class is
        never written, which is exactly how 2026-08-06 went sideways.
        """
        skus_by_class = {}
        skipped = []
        for product in products:
            tax = product._magento_sale_tax()
            if not tax:
                skipped.append({'sku': product.default_code, 'reason': 'no sale tax'})
                continue
            tax_class = tax.magento_tax_class_id
            if not tax_class:
                skipped.append({
                    'sku': product.default_code,
                    'reason': 'tax "%s" is not mapped to a Magento tax class' % tax.name,
                })
                continue
            skus_by_class.setdefault(tax_class.magento_class_id, []).append(
                product.default_code)

        client = self.env['artaza.magento.client']
        not_found = []
        written = 0
        for class_id, skus in skus_by_class.items():
            missing = client.write_product_tax_class(class_id, skus)
            not_found.extend(missing)
            written += len(skus) - len(missing)

        if skipped:
            _logger.info("Magento tax push skipped: %s", skipped)
        failures = [{'ref': item['sku'], 'reason': item['reason']} for item in skipped]
        failures += [{
            'ref': sku,
            'reason': self.env._("Magento does not know that SKU."),
        } for sku in not_found]
        failed_skus = {item['ref'] for item in failures}
        return {
            'written': written,
            'skipped': skipped,
            'not_found': not_found,
            'ok_skus': [sku for skus in skus_by_class.values() for sku in skus
                        if sku not in failed_skus],
            'failures': failures,
        }

    @api.model
    def _magento_track(self, tracker, result):
        """Feed a push result into the sync history.

        A push can succeed at the HTTP level and still have left work undone
        (an unmapped warehouse, a SKU Magento does not know, a price Magento
        rejected). Those land as failure lines so the run shows up as
        **partial** instead of quietly passing as a success.
        """
        result = result or {}
        for failure in result.get('failures') or []:
            tracker.fail(failure.get('ref'), failure.get('reason'))
        tracker.ok(result.get('ok_skus') or [])

    # ── Button: single-product sync (stock + price + tax) ──────
    def magento_sync_now(self):
        """Push this product's stock, price and tax class, and clear its flags."""
        self.ensure_one()
        log = self.env['artaza.magento.sync.log']
        with log.track('stock', 'out', 'ui') as tracker:
            tracker.attempt(self.default_code)
            stock_result = self._magento_push_stock(self)
            self._magento_track(tracker, stock_result)
        with log.track('price', 'out', 'ui') as tracker:
            tracker.attempt(self.default_code)
            self._magento_track(tracker, self._magento_push_price(self))
        with log.track('tax', 'out', 'ui') as tracker:
            tracker.attempt(self.default_code)
            self._magento_track(tracker, self._magento_push_tax(self))
        self.sudo().write({
            'magento_stock_dirty': False,
            'magento_price_dirty': False,
            'magento_tax_dirty': False,
        })
        return stock_result  # the front reads `skipped` (pending warehouses)

    @api.model
    def magento_mark_all_dirty(self):
        """Mark all syncable products as pending (stock + price + tax)."""
        products = self.search([('is_storable', '=', True), ('default_code', '!=', False)])
        products.write({
            'magento_stock_dirty': True,
            'magento_price_dirty': True,
            'magento_tax_dirty': True,
        })
        return len(products)

    # ── Cron ───────────────────────────────────────────────────
    @api.model
    def _cron_magento_sync_stock(self):
        """Cron: push stock, price and tax class of the pending products."""
        icp = self.env['ir.config_parameter'].sudo()
        batch_size = int(icp.get_param('artaza_magento_connect.stock_batch_size') or 50)
        self._magento_cron_push(
            'magento_stock_dirty', self._magento_push_stock, batch_size, 'stock')
        self._magento_cron_push(
            'magento_price_dirty', self._magento_push_price, batch_size, 'price')
        self._magento_cron_push(
            'magento_tax_dirty', self._magento_push_tax, batch_size, 'tax')

    @api.model
    def _magento_cron_push(self, dirty_field, push_fn, batch_size, operation):
        """Process products with `dirty_field=True` in batches using `push_fn`.

        Idempotent: quantity/price are absolute per SKU. On a batch error it
        stops and leaves the pending ones for the next tick — but the failure is
        now **recorded in the sync history** instead of only reaching the
        container log. That silence is what made the 2026-08-06 tax rollout take
        three rounds to diagnose (see integration_v4.md §2).
        """
        sent = 0
        log = self.env['artaza.magento.sync.log']
        for _batch in range(10000):  # guard against an infinite loop
            products = self.search([
                (dirty_field, '=', True),
                ('is_storable', '=', True),
                ('default_code', '!=', False),
            ], limit=batch_size)
            if not products:
                break
            try:
                with log.track(operation, 'out', 'cron') as tracker:
                    tracker.attempt(products.mapped('default_code'))
                    self._magento_track(tracker, push_fn(products))
            except Exception as exc:  # noqa: BLE001 - leave pending for the next tick
                _logger.warning(
                    "Magento cron (%s): batch failed, will retry later: %s",
                    dirty_field, exc,
                )
                break
            products.write({dirty_field: False})
            sent += len(products)

        if sent:
            _logger.info("Magento cron (%s): %s product(s) synced.", dirty_field, sent)
