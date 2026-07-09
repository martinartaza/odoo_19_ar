import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


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
                qty_per_loc[(product_id, location_id)] = group['quantity']

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
    @api.model
    def _magento_push_stock(self, products):
        """Push the (per-warehouse) stock of `products` to the middleware."""
        warehouses = self.env['stock.warehouse'].search([], order='id')
        qty_by_wh = self._magento_qty_by_warehouse(products, warehouses)
        payload = [{
            'sku': product.default_code,
            'warehouse_code': wh.code,
            'qty': qty_by_wh[product.id].get(wh.code, 0.0),
        } for product in products for wh in warehouses]
        return self.env['artaza.magento.connector'].call('POST', 'stock', payload)

    @api.model
    def _magento_push_price(self, products):
        """Push the base price (list_price) of `products` to the middleware."""
        payload = [
            {'sku': product.default_code, 'price': product.list_price}
            for product in products
        ]
        return self.env['artaza.magento.connector'].call('POST', 'prices', payload)

    # ── Button: single-product sync (stock + price) ────────────
    def magento_sync_now(self):
        """Push this product's stock and price and clear its flags."""
        self.ensure_one()
        stock_result = self._magento_push_stock(self)
        self._magento_push_price(self)
        self.sudo().write({
            'magento_stock_dirty': False,
            'magento_price_dirty': False,
        })
        return stock_result  # the front reads `skipped` (pending warehouses)

    @api.model
    def magento_mark_all_dirty(self):
        """Mark all syncable products as pending (stock + price)."""
        products = self.search([('is_storable', '=', True), ('default_code', '!=', False)])
        products.write({'magento_stock_dirty': True, 'magento_price_dirty': True})
        return len(products)

    # ── Cron ───────────────────────────────────────────────────
    @api.model
    def _cron_magento_sync_stock(self):
        """Cron: push stock and price of the pending products, in batches."""
        icp = self.env['ir.config_parameter'].sudo()
        batch_size = int(icp.get_param('artaza_magento_connect.stock_batch_size') or 50)
        self._magento_cron_push('magento_stock_dirty', self._magento_push_stock, batch_size)
        self._magento_cron_push('magento_price_dirty', self._magento_push_price, batch_size)

    @api.model
    def _magento_cron_push(self, dirty_field, push_fn, batch_size):
        """Process products with `dirty_field=True` in batches using `push_fn`.

        Idempotent: quantity/price are absolute per SKU. On a batch error it
        stops and leaves the pending ones for the next tick.
        """
        sent = 0
        for _batch in range(10000):  # guard against an infinite loop
            products = self.search([
                (dirty_field, '=', True),
                ('is_storable', '=', True),
                ('default_code', '!=', False),
            ], limit=batch_size)
            if not products:
                break
            try:
                push_fn(products)
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
