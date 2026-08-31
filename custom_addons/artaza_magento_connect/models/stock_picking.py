import logging

from odoo import api, fields, models

from .magento_normalize import is_fully_invoiced, is_fully_shipped

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

        log = self.env['artaza.magento.sync.log']
        try:
            with log.track('shipment', 'out', 'cron') as tracker:
                tracker.attempt(order.magento_order_id)
                self._magento_ship_order(order.magento_order_entity_id)
                tracker.ok(order.magento_order_id)
        except Exception as exc:  # noqa: BLE001 - never block the Odoo delivery
            _logger.warning(
                "Magento shipment push failed for order %s: %s",
                order.magento_order_id, exc,
            )
            return
        self.magento_shipment_done = True

    @api.model
    def _magento_ship_order(self, magento_order_id):
        """Ship a Magento order in full, invoicing first if it is still pending.

        Magento refuses to ship an order it has not invoiced, so the two steps
        travel together. Both halves are idempotent: an already-invoiced or
        already-shipped order is a no-op, which is what makes a retry safe.

        The invoice raised here carries Magento's ORIGINAL total on purpose. The
        fiscal document is the Odoo invoice with the negotiated total; this one
        exists only so Magento will let the shipment through.
        """
        client = self.env['artaza.magento.client']
        order = client.get_order(magento_order_id)
        if not is_fully_invoiced(order):
            client.create_invoice(magento_order_id, notify=False)
            order = client.get_order(magento_order_id)  # refresh before shipping
        if is_fully_shipped(order):
            return 0
        return client.create_shipment(magento_order_id, notify=True)
