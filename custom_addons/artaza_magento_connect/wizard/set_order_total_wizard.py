from odoo import fields, models
from odoo.exceptions import UserError


class SaleTotalWizard(models.TransientModel):
    """Let the user type the desired order total; back-calculate the lines to reach it.

    Odoo computes forward (unit price × qty − discount → total); there is no
    native "type the total". This wizard scales each line's tax-included unit
    price by (new_total / current_total) and absorbs the rounding residual on the
    last line, so `amount_total` lands EXACTLY on `new_total`. (A uniform discount
    % would round to 2 decimals and miss an arbitrary target by a few pesos.)
    A factor < 1 is a discount, > 1 a surcharge (e.g. a fee for a 90-day cheque).
    """

    _name = 'artaza.sale.total.wizard'
    _description = 'Adjust order total (back-calculates the line discount/surcharge)'

    order_id = fields.Many2one('sale.order', required=True, ondelete='cascade')
    currency_id = fields.Many2one(related='order_id.currency_id')
    current_total = fields.Monetary(
        string="Current total", related='order_id.amount_total', readonly=True,
    )
    new_total = fields.Monetary(string="New total", required=True)
    reason = fields.Char(
        string="Reason", required=True,
        help="Why the total changed (e.g. volume discount, surcharge for a "
             "90-day cheque).",
    )

    def action_apply(self):
        self.ensure_one()
        order = self.order_id
        if self.new_total <= 0:
            raise UserError(self.env._("The new total must be greater than zero."))
        lines = order.order_line.filtered(
            lambda line: not line.display_type and line.product_uom_qty
        )
        base = sum(line.price_unit * line.product_uom_qty for line in lines)
        if not lines or base <= 0:
            raise UserError(self.env._("The order has no priced lines to adjust."))
        factor = self.new_total / base
        for line in lines:
            line.write({'discount': 0.0, 'price_unit': line.price_unit * factor})
        # Absorb the per-line rounding residual on the last line → exact total.
        residual = self.new_total - order.amount_total
        if residual:
            last = lines[-1]
            last.price_unit = last.price_unit + (residual / last.product_uom_qty)
        order.magento_adjustment_reason = self.reason
        return {'type': 'ir.actions.act_window_close'}
