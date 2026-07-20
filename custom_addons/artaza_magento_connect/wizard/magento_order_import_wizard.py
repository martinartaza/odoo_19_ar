from odoo import fields, models


class MagentoOrderImportWizard(models.TransientModel):
    """Import a single Magento order by number.

    Checks if it is already in Odoo; if so, offers a link to view it. If not, it
    fetches that order from Magento (through the middleware, bypassing the cursor
    and the payment-method gate) and imports it — reporting the middleware's
    response when it cannot.
    """
    _name = 'magento.order.import.wizard'
    _description = 'Import a Magento order by number'

    order_number = fields.Char(string="Magento Order Number", required=True)
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('exists', "Already imported"),
            ('imported', "Imported"),
            ('not_found', "Not found"),
            ('error', "Error"),
        ],
        default='draft', readonly=True,
    )
    sale_order_id = fields.Many2one('sale.order', string="Odoo Order", readonly=True)
    message = fields.Text(string="Result", readonly=True)

    def action_import(self):
        """Check-or-import the order and re-open the wizard with the result."""
        self.ensure_one()
        result = self.env['sale.order']._magento_import_one(self.order_number)
        status = result.get('status')
        order = result.get('order')

        vals = {'state': status, 'sale_order_id': order.id if order else False}
        if status == 'exists':
            vals['message'] = self.env._(
                "This order is already in Odoo as %s.", order.name
            )
        elif status == 'imported':
            vals['message'] = self.env._("Imported as %s.", order.name)
        else:
            vals['message'] = result.get('message') or self.env._("Could not import.")
        self.write(vals)

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': self.env.context,
        }

    def action_view_order(self):
        """Open the linked Odoo sales order."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }
