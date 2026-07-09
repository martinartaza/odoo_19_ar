from odoo import api, models


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    def _magento_mark_products_dirty(self):
        """Mark the syncable products of these quants as pending."""
        products = self.product_id.filtered(
            lambda p: p.is_storable and p.default_code and not p.magento_stock_dirty
        )
        if products:
            products.sudo().write({'magento_stock_dirty': True})

    @api.model_create_multi
    def create(self, vals_list):
        quants = super().create(vals_list)
        quants._magento_mark_products_dirty()
        return quants

    def write(self, vals):
        res = super().write(vals)
        # Only on-hand matters to us (not reservations).
        if 'quantity' in vals:
            self._magento_mark_products_dirty()
        return res

    def unlink(self):
        products = self.product_id.filtered(lambda p: p.is_storable and p.default_code)
        res = super().unlink()
        if products:
            products.sudo().write({'magento_stock_dirty': True})
        return res
