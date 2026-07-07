import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    magento_stock_dirty = fields.Boolean(
        string="Stock pendiente de sync a Magento",
        default=False, index=True, copy=False,
        help="Se marca cuando cambia el stock; el cron lo empuja y lo limpia.",
    )
    magento_price_dirty = fields.Boolean(
        string="Precio pendiente de sync a Magento",
        default=False, index=True, copy=False,
        help="Se marca cuando cambia el precio de venta; el cron lo empuja.",
    )

    # ── Cálculo de stock por bodega ────────────────────────────
    @api.model
    def _magento_qty_by_warehouse(self, products, warehouses):
        """{product_id: {warehouse_code: on_hand}} leyendo stock.quant.

        Discrimina por bodega sumando las ubicaciones internas de cada una
        (stock location + hijas). No usa el context `warehouse` de
        `qty_available` porque no separa bien por depósito.
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

    # ── Grilla del módulo ──────────────────────────────────────
    @api.model
    def magento_stock_matrix(self, search=None, offset=0, limit=50):
        """Matriz de stock por bodega (paginada) para el sync unitario."""
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

    # ── Push helpers (usados por el botón y el cron) ───────────
    @api.model
    def _magento_push_stock(self, products):
        """Empuja el stock (por bodega) de `products` al middleware."""
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
        """Empuja el precio base (list_price) de `products` al middleware."""
        payload = [
            {'sku': product.default_code, 'price': product.list_price}
            for product in products
        ]
        return self.env['artaza.magento.connector'].call('POST', 'prices', payload)

    # ── Botón: sync unitario (stock + precio) ──────────────────
    def magento_sync_now(self):
        """Empuja stock y precio de este producto y limpia sus flags."""
        self.ensure_one()
        stock_result = self._magento_push_stock(self)
        self._magento_push_price(self)
        self.sudo().write({
            'magento_stock_dirty': False,
            'magento_price_dirty': False,
        })
        return stock_result  # el front lee `skipped` (bodegas pendientes)

    @api.model
    def magento_mark_all_dirty(self):
        """Marca todos los productos sincronizables como pendientes (stock + precio)."""
        products = self.search([('is_storable', '=', True), ('default_code', '!=', False)])
        products.write({'magento_stock_dirty': True, 'magento_price_dirty': True})
        return len(products)

    # ── Cron ───────────────────────────────────────────────────
    @api.model
    def _cron_magento_sync_stock(self):
        """Cron: empuja stock y precio de los productos pendientes, en lotes."""
        icp = self.env['ir.config_parameter'].sudo()
        batch_size = int(icp.get_param('artaza_magento_connect.stock_batch_size') or 50)
        self._magento_cron_push('magento_stock_dirty', self._magento_push_stock, batch_size)
        self._magento_cron_push('magento_price_dirty', self._magento_push_price, batch_size)

    @api.model
    def _magento_cron_push(self, dirty_field, push_fn, batch_size):
        """Procesa en lotes los productos con `dirty_field=True` usando `push_fn`.

        Idempotente: la cantidad/precio son absolutos por SKU. Ante un error de
        un lote corta y deja los pendientes para el próximo tick.
        """
        sent = 0
        for _batch in range(10000):  # guarda contra loop infinito
            products = self.search([
                (dirty_field, '=', True),
                ('is_storable', '=', True),
                ('default_code', '!=', False),
            ], limit=batch_size)
            if not products:
                break
            try:
                push_fn(products)
            except Exception as exc:  # noqa: BLE001 - dejar pendientes para el próximo tick
                _logger.warning(
                    "Cron Magento (%s): lote falló, se reintenta luego: %s",
                    dirty_field, exc,
                )
                break
            products.write({dirty_field: False})
            sent += len(products)

        if sent:
            _logger.info("Cron Magento (%s): %s producto(s) sincronizado(s).", dirty_field, sent)
