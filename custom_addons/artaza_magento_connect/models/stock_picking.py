import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    """Push the shipment to Magento when an outgoing delivery is validated.

    When the user validates the delivery of a Magento-originated sale order,
    tell Magento to invoice (prerequisite) and ship the order → it moves to
    `complete` and releases its reservation. Display-only balances are untouched;
    the fiscal document is the Odoo invoice. Full shipments only.
    """
    _inherit = 'stock.picking'

    magento_shipment_done = fields.Boolean(
        string="Sent to Magento", copy=False, readonly=True,
        help="The shipment was already pushed to Magento (avoids duplicates).",
    )

    def _action_done(self):
        # Runs when the picking is actually validated (after any wizard).
        res = super()._action_done()
        for picking in self:
            picking._magento_push_shipment()
        return res

    def _magento_push_shipment(self):
        """Push a full shipment to Magento for this delivery, once."""
        self.ensure_one()
        if self.picking_type_code != 'outgoing' or self.state != 'done':
            return
        if self.magento_shipment_done:
            return
        order = self.sale_id
        if not order or not order.magento_order_entity_id:
            return  # not a Magento order

        try:
            self.env['artaza.magento.connector'].call('POST', 'shipments', {
                'order_id': order.magento_order_entity_id,
                'notify': True,
            })
            self.magento_shipment_done = True
        except Exception as exc:  # noqa: BLE001 - never block the Odoo delivery
            _logger.warning(
                "Magento shipment push failed for order %s: %s",
                order.magento_order_id, exc,
            )
