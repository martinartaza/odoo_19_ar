from odoo import api, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

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

        # qty on-hand por bodega (una lectura batch por bodega)
        qty_by_wh = {}
        for wh in warehouses:
            quantities = products.with_context(warehouse=wh.id).mapped('qty_available')
            qty_by_wh[wh.code] = dict(zip(products.ids, quantities))

        rows = []
        for product in products:
            qtys = {wh.code: qty_by_wh[wh.code].get(product.id, 0.0) for wh in warehouses}
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
        payload = [{
            'sku': self.default_code,
            'warehouse_code': wh.code,
            'qty': self.with_context(warehouse=wh.id).qty_available,
        } for wh in warehouses]
        return self.env['artaza.magento.connector'].call('POST', 'stock', payload)
