from odoo import models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def write(self, vals):
        res = super().write(vals)
        # Sales price changed → mark the variants to re-sync.
        if 'list_price' in vals:
            variants = self.product_variant_ids.filtered(
                lambda p: p.is_storable and p.default_code and not p.magento_price_dirty
            )
            if variants:
                variants.sudo().write({'magento_price_dirty': True})
        return res
