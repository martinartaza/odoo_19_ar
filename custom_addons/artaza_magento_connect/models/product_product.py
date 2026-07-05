from odoo import api, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.model
    def magento_stock_matrix(self, search=None):
        """Matriz de stock por bodega para la pantalla de sync unitario.

        Devuelve `{warehouses: [{code, name}], rows: [{id, sku, name, qtys, total}]}`
        con una columna por bodega (dinámico según cuántas haya).
        """
        warehouses = self.env['stock.warehouse'].search([], order='id')

        domain = [('is_storable', '=', True), ('default_code', '!=', False)]
        if search:
            domain += ['|', ('name', 'ilike', search), ('default_code', 'ilike', search)]
        products = self.search(domain, order='default_code', limit=200)

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
