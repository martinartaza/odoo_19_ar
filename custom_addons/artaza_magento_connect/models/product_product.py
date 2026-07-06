from odoo import api, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

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

        # qty por (producto, ubicación)
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

    @api.model
    def magento_stock_matrix(self, search=None, offset=0, limit=50):
        """Matriz de stock por bodega (paginada) para el sync unitario.

        Devuelve `{warehouses, rows, total, offset, limit}` con una columna por
        bodega (dinámico). `total` es el conteo completo para paginar en el front.
        """
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

    def magento_sync_stock(self):
        """Empuja el stock de este producto (por bodega) al middleware /stock."""
        self.ensure_one()
        warehouses = self.env['stock.warehouse'].search([], order='id')
        qty_by_wh = self._magento_qty_by_warehouse(self, warehouses)[self.id]
        payload = [{
            'sku': self.default_code,
            'warehouse_code': wh.code,
            'qty': qty_by_wh.get(wh.code, 0.0),
        } for wh in warehouses]
        return self.env['artaza.magento.connector'].call('POST', 'stock', payload)
