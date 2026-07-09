import logging
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CURSOR_PARAM = 'artaza_magento_connect.orders_cursor'
PROCESSING_METHODS_PARAM = 'artaza_magento_connect.processing_methods'
PENDING_METHODS_PARAM = 'artaza_magento_connect.pending_methods'
DEFAULT_CURSOR = '2000-01-01 00:00:00'
PULL_STATES = 'new,processing,complete'
PAGE_SIZE = 50


def _split_methods(value):
    """Parse a comma-separated list of Magento payment method codes."""
    return [code.strip() for code in (value or '').split(',') if code.strip()]


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    magento_order_id = fields.Char(
        string="Magento Order ID", index=True, copy=False, readonly=True,
    )
    magento_order_state = fields.Char(
        string="Magento State", copy=False, readonly=True,
    )
    magento_order_entity_id = fields.Integer(
        string="Magento Entity ID", copy=False, readonly=True,
    )
    magento_order_total = fields.Monetary(
        string="Original Magento Total", copy=False, readonly=True,
        help="Grand total of the order in Magento when absorbed; base for the adjustment.",
    )
    magento_adjustment_reason = fields.Char(
        string="Adjustment Reason (Magento)", copy=False,
        help="Shown to the customer in their Magento order next to the agreed total.",
    )

    _sql_constraints = [
        ('magento_order_id_uniq', 'unique(magento_order_id)',
         "A sales order with that Magento ID already exists."),
    ]

    # ── Cron: pull orders from Magento ─────────────────────────
    @api.model
    def _cron_magento_pull_orders(self):
        """Pull new/updated orders from Magento (cursor-based)."""
        icp = self.env['ir.config_parameter'].sudo()
        processing_methods = _split_methods(icp.get_param(PROCESSING_METHODS_PARAM))
        pending_methods = _split_methods(icp.get_param(PENDING_METHODS_PARAM))
        connector = self.env['artaza.magento.connector']

        for _page in range(1000):  # guard against an infinite loop
            cursor = icp.get_param(CURSOR_PARAM) or DEFAULT_CURSOR
            endpoint = 'orders?updated_since=%s&page_size=%s&states=%s' % (
                quote(cursor), PAGE_SIZE, quote(PULL_STATES),
            )
            result = connector.call('GET', endpoint)
            orders = result.get('orders', [])
            if not orders:
                break

            for order in orders:
                try:
                    self._magento_absorb_order(order, processing_methods, pending_methods)
                except Exception as exc:  # noqa: BLE001 - log and continue with the next one
                    _logger.warning(
                        "Magento order %s failed: %s", order.get('increment_id'), exc,
                    )
                # advance the cursor even if an order failed (create-once is idempotent)
                if order.get('updated_at'):
                    icp.set_param(CURSOR_PARAM, order['updated_at'])

            if len(orders) < PAGE_SIZE:
                break

    # ── Absorb a single order ──────────────────────────────────
    @api.model
    def _magento_absorb_order(self, order, processing_methods, pending_methods):
        state = order.get('state')
        method = order.get('payment_method')
        # Magento state: 'processing'/'complete' = paid; 'new' (status pending) = placed, unpaid.
        # Two method whitelists: paid ones import as confirmed sales (no price change); pending
        # ones import as editable quotations (negotiable; price can be pushed back to Magento).
        paid = state in ('processing', 'complete') and method in processing_methods
        negotiable = state == 'new' and method in pending_methods
        if not (paid or negotiable):
            return  # unlisted method / online in-flight (pending_payment) → not absorbable

        # create-once: if it already exists, Odoo owns it and we do not overwrite it
        existing = self.search([('magento_order_id', '=', order['increment_id'])], limit=1)
        if existing:
            return existing

        partner = self._magento_upsert_partner(order)

        line_commands = []
        prices = []
        for item in order.get('items', []):
            product = self.env['product.product'].search(
                [('default_code', '=', item['sku'])], limit=1,
            )
            if not product:
                _logger.warning(
                    "Order %s: SKU %s does not exist in Odoo, line skipped",
                    order['increment_id'], item['sku'],
                )
                continue
            line_commands.append((0, 0, {
                'product_id': product.id,
                'product_uom_qty': item.get('qty') or 0.0,
                'price_unit': item.get('price') or 0.0,
                'name': item.get('name') or product.display_name,
            }))
            prices.append(item.get('price') or 0.0)

        so = self.create({
            'partner_id': partner.id,
            'order_line': line_commands,
            'magento_order_id': order['increment_id'],
            'magento_order_state': state,
            'magento_order_entity_id': order.get('entity_id'),
            'magento_order_total': order.get('grand_total') or 0.0,
        })

        # Odoo owns the amount: force the Magento price (prevents the pricelist
        # from recomputing price_unit when the line is created).
        for line, price in zip(so.order_line, prices):
            if line.price_unit != price:
                line.price_unit = price

        if paid:
            so.action_confirm()  # paid → sales order; offline pending → stays a quotation
        return so

    # ── Push the negotiated adjustment to Magento (display-only) ─
    def action_magento_push_negotiation(self):
        """Send Magento the agreed adjustment/total to show it to the customer.

        Does not touch Magento balances: it writes informational fields. The
        adjustment is the delta against the original order total in Magento.
        """
        self.ensure_one()
        if not self.magento_order_entity_id:
            raise UserError(self.env._("This order does not come from Magento."))

        adjustment = self.amount_total - (self.magento_order_total or 0.0)
        self.env['artaza.magento.connector'].call('POST', 'negotiation', {
            'order_id': self.magento_order_entity_id,
            'adjustment_amount': adjustment,
            'adjustment_reason': self.magento_adjustment_reason or False,
            'negotiation_total': self.amount_total,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Adjustment sent to Magento"),
                'message': self.env._("The customer will see the agreed total in their order."),
                'sticky': False,
            },
        }

    # ── Upsert the customer (by email) ─────────────────────────
    @api.model
    def _magento_upsert_partner(self, order):
        customer = order.get('customer') or {}
        billing = order.get('billing') or {}
        Partner = self.env['res.partner']

        email = customer.get('email')
        if email:
            partner = Partner.search([('email', '=', email)], limit=1)
            if partner:
                return partner

        name = ' '.join(filter(None, [
            customer.get('firstname') or billing.get('firstname'),
            customer.get('lastname') or billing.get('lastname'),
        ])).strip() or email or 'Magento Customer'

        country = self.env['res.country'].search(
            [('code', '=', billing.get('country_id'))], limit=1,
        ) if billing.get('country_id') else self.env['res.country']

        state = self.env['res.country.state']
        if country and billing.get('region'):
            state = state.search([
                ('country_id', '=', country.id),
                ('name', '=', billing['region']),
            ], limit=1)

        return Partner.create({
            'name': name,
            'email': email or False,
            'phone': billing.get('telephone') or False,
            'street': billing.get('street') or False,
            'city': billing.get('city') or False,
            'zip': billing.get('postcode') or False,
            'country_id': country.id or False,
            'state_id': state.id or False,
            'vat': billing.get('vat_id') or False,
        })
