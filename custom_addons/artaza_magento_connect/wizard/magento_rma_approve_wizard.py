from odoo import fields, models


class MagentoRmaApproveWizard(models.TransientModel):
    """Restock decision on approving an RMA product.

    The product passed inspection, but whether it re-enters sellable stock is a
    human call: a wrong-color item is resellable; a factory-defective one is not.
    """
    _name = 'magento.rma.approve.wizard'
    _description = 'RMA approve: restock decision'

    rma_id = fields.Many2one(
        'magento.rma', string="RMA", required=True, ondelete='cascade',
    )
    restock = fields.Selection(
        [
            ('sellable', "Vuelve a depósito (revendible)"),
            ('scrap', "No vuelve a depósito (dañado / descartado)"),
        ],
        string="¿El producto vuelve a depósito?",
        required=True,
        default='sellable',
        help="Revendible: entra a stock para vender. "
             "Dañado/descartado: se recibe pero va a descarte (no se vende).",
    )

    def action_confirm(self):
        self.ensure_one()
        self.rma_id._approve_with_restock(self.restock)
        return {'type': 'ir.actions.act_window_close'}
