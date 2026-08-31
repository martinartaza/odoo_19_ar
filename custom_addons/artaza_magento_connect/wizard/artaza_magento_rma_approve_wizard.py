from odoo import fields, models


class MagentoRmaApproveWizard(models.TransientModel):
    """Restock decision on approving an RMA product.

    The product passed inspection, but whether it re-enters sellable stock is a
    human call: a wrong-color item is resellable; a factory-defective one is not.
    """
    _name = 'artaza.magento.rma.approve.wizard'
    _description = 'RMA approve: restock decision'

    rma_id = fields.Many2one(
        'artaza.magento.rma', string="RMA", required=True, ondelete='cascade',
    )
    restock = fields.Selection(
        [
            ('sellable', "Back to stock (sellable again)"),
            ('scrap', "Not back to stock (damaged / written off)"),
        ],
        string="Does the product go back to stock?",
        required=True,
        default='sellable',
        help="Sellable: it re-enters stock and can be sold again. "
             "Damaged/written off: it is received but goes to scrap, not to sale.",
    )

    def action_confirm(self):
        self.ensure_one()
        self.rma_id._approve_with_restock(self.restock)
        return {'type': 'ir.actions.act_window_close'}
