from odoo import fields, models
from odoo.exceptions import UserError


class SaleTotalWizard(models.TransientModel):
    """Let the user type the desired order total and back-calculate the line
    discount/surcharge to reach it.

    Odoo computes forward (unit price × qty − discount → total); there is no
    native "type the total". This wizard writes a uniform discount % on the
    order lines so `amount_total` lands on `new_total`. A negative discount is a
    surcharge (e.g. a fee for a 90-day cheque), so one field covers both cases.
    The unit price is tax-included in this setup, so the total scales linearly
    with the discount.
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
        # Base = total with NO discount. Unit price is tax-included here, so the
        # order total scales linearly with a uniform discount %.
        base = sum(line.price_unit * line.product_uom_qty for line in lines)
        if base <= 0:
            raise UserError(self.env._("The order has no priced lines to adjust."))
        # Negative discount = surcharge; one field handles discount and increase.
        discount = (1.0 - (self.new_total / base)) * 100.0
        lines.write({'discount': discount})
        order.magento_adjustment_reason = self.reason
        return {'type': 'ir.actions.act_window_close'}
