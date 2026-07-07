from odoo import models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        res = super().write(vals)
        # Cambió el precio de venta → marcar las variantes para re-sincronizar.
        if 'list_price' in vals:
            variants = self.product_variant_ids.filtered(
                lambda p: p.is_storable and p.default_code and not p.magento_price_dirty
            )
            if variants:
                variants.sudo().write({'magento_price_dirty': True})
        return res
