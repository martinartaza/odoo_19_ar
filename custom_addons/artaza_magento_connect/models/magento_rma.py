import logging
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CURSOR_PARAM = 'artaza_magento_connect.rmas_cursor'
DEFAULT_CURSOR = '2000-01-01 00:00:00'
PAGE_SIZE = 50

# Local workflow states — kept identical to Magento's closed status set so the
# push is a simple 1:1 (integration_v3.md §7.1). Magento only mirrors them.
STATES = [
    ('requested', "Requested"),
    ('accepted', "Accepted"),
    ('rejected', "Rejected"),
    ('in_transit', "In transit"),
    ('inspection', "Received / under inspection"),
    ('approved', "Approved (product OK)"),
    ('fraud', "Fraud / tampered"),
    ('resolved_exchange', "Resolved: exchange"),
    ('resolved_credit', "Resolved: credit"),
    ('returned', "Returned to customer"),
    ('held', "Held"),
]
VALID_STATES = {code for code, _label in STATES}


class MagentoRma(models.Model):
    """Return request produced in Magento (Artaza_Rma), pulled into Odoo.

    This is a thin "inbox": Magento is where the customer opens the return and
    Odoo has no native place for a web return request, so we land it here. The
    real work (return picking, credit note) is done with native Odoo tools; the
    decision is pushed back later. Create-once by ``magento_increment_id``:
    Odoo owns the record after import (integration_v3.md §7.4).
    """
    _name = 'magento.rma'
    _description = 'Magento RMA (return request)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'magento_increment_id'
    _order = 'id desc'

    magento_rma_id = fields.Integer(
        string="Magento RMA ID", index=True, copy=False, readonly=True,
        help="Magento rma entity_id — the target of the status push.",
    )
    magento_increment_id = fields.Char(
        string="RMA Number", index=True, copy=False, readonly=True,
    )
    magento_order_increment_id = fields.Char(
        string="Order Number", copy=False, readonly=True,
    )
    magento_order_entity_id = fields.Integer(
        string="Magento Order ID", copy=False, readonly=True,
    )
    sale_order_id = fields.Many2one(
        'sale.order', string="Sales Order", copy=False, readonly=True,
        help="The Odoo sales order this return belongs to, if it was imported.",
    )
    partner_id = fields.Many2one(
        'res.partner', string="Customer", copy=False, readonly=True,
    )
    customer_email = fields.Char(string="Customer Email", copy=False, readonly=True)
    rma_type = fields.Selection(
        [('defective', "Defective product"), ('exchange', "Exchange for another product")],
        string="Type", copy=False, readonly=True,
    )
    magento_status = fields.Char(
        string="Magento Status", copy=False, readonly=True,
        help="Status as shown in Magento (requested, accepted, …).",
    )
    reason_code = fields.Char(string="Reason", copy=False, readonly=True)
    customer_note = fields.Text(string="Customer Note", copy=False, readonly=True)

    # ── Local workflow + operator-filled fields (pushed on the decision) ──
    state = fields.Selection(
        STATES, string="Status", default='requested', copy=False, tracking=True,
        help="Local workflow state. Each transition is pushed to Magento.",
    )
    admin_message = fields.Text(
        string="Message to customer", copy=False, tracking=True,
        help="Shown to the customer in Magento (e.g. the rejection reason).",
    )
    inspection_note = fields.Text(
        string="Inspection note (internal)", copy=False,
        help="Operator's assessment on receipt. Not shown to the customer.",
    )
    resolution = fields.Char(string="Resolution", copy=False, readonly=True)
    credit_amount = fields.Float(string="Credit Amount", copy=False)
    coupon_code = fields.Char(string="Coupon Code", copy=False)
    odoo_reference = fields.Char(
        string="Odoo Reference", copy=False,
        help="Traceability reference sent to Magento (e.g. the credit note number).",
    )
    magento_created_at = fields.Char(string="Requested At", copy=False, readonly=True)
    magento_updated_at = fields.Char(string="Updated At", copy=False, readonly=True)
    line_ids = fields.One2many(
        'magento.rma.line', 'rma_id', string="Lines", readonly=True,
    )

    _sql_constraints = [
        ('magento_increment_id_uniq', 'unique(magento_increment_id)',
         "A Magento RMA with that number already exists."),
    ]

    # ── Cron: pull RMAs from Magento ───────────────────────────
    @api.model
    def _cron_magento_pull_rmas(self):
        """Pull new/updated RMAs from Magento (cursor-based). Returns the count
        of newly created records (used by the manual "Import now" button)."""
        icp = self.env['ir.config_parameter'].sudo()
        connector = self.env['artaza.magento.connector']
        created = 0

        for _page in range(1000):  # guard against an infinite loop
            cursor = icp.get_param(CURSOR_PARAM) or DEFAULT_CURSOR
            endpoint = 'rma?updated_since=%s&page_size=%s' % (quote(cursor), PAGE_SIZE)
            result = connector.call('GET', endpoint)
            rmas = result.get('rmas', [])
            if not rmas:
                break

            for rma in rmas:
                try:
                    if self._magento_absorb_rma(rma):
                        created += 1
                except Exception as exc:  # noqa: BLE001 - log and continue with the next one
                    _logger.warning(
                        "Magento RMA %s failed: %s", rma.get('increment_id'), exc,
                    )
                # advance the cursor even if one failed (create-once is idempotent)
                if rma.get('updated_at'):
                    icp.set_param(CURSOR_PARAM, rma['updated_at'])

            if len(rmas) < PAGE_SIZE:
                break

        return created

    # ── Absorb a single RMA ────────────────────────────────────
    @api.model
    def _magento_absorb_rma(self, rma):
        """Create-once the RMA in Odoo. Returns the new record, or False when it
        already existed (Odoo owns it and we do not overwrite it)."""
        increment_id = rma.get('increment_id')
        if not increment_id:
            return False
        if self.search_count([('magento_increment_id', '=', increment_id)]):
            return False

        sale_order = self.env['sale.order'].search(
            [('magento_order_id', '=', rma.get('order_increment_id'))], limit=1,
        )
        partner = sale_order.partner_id or self._magento_resolve_partner(rma.get('customer_email'))

        line_commands = []
        for item in rma.get('items', []):
            sku = item.get('sku')
            if not sku:
                continue
            product = self.env['product.product'].search(
                [('default_code', '=', sku)], limit=1,
            )
            line_commands.append((0, 0, {
                'sku': sku,
                'product_id': product.id or False,
                'name': item.get('name') or (product.display_name if product else sku),
                'qty_requested': item.get('qty_requested') or 0.0,
                'magento_order_item_id': item.get('order_item_id') or 0,
            }))

        rma_type = rma.get('type')
        return self.create({
            'magento_rma_id': rma.get('rma_id'),
            'magento_increment_id': increment_id,
            'magento_order_increment_id': rma.get('order_increment_id'),
            'magento_order_entity_id': rma.get('order_id'),
            'sale_order_id': sale_order.id or False,
            'partner_id': partner.id if partner else False,
            'customer_email': rma.get('customer_email'),
            'rma_type': rma_type if rma_type in ('defective', 'exchange') else False,
            'magento_status': rma.get('status'),
            'state': rma.get('status') if rma.get('status') in VALID_STATES else 'requested',
            'reason_code': rma.get('reason_code'),
            'customer_note': rma.get('customer_note'),
            'resolution': rma.get('resolution'),
            'credit_amount': rma.get('credit_amount') or 0.0,
            'coupon_code': rma.get('coupon_code'),
            'magento_created_at': rma.get('created_at'),
            'magento_updated_at': rma.get('updated_at'),
            'line_ids': line_commands,
        })

    @api.model
    def _magento_resolve_partner(self, email):
        if not email:
            return self.env['res.partner']
        return self.env['res.partner'].search([('email', '=', email)], limit=1)

    # ── Workflow: decide and push the status back to Magento ───
    def _push_status(self, status, resolution=None, admin_message=None,
                     credit_amount=None, coupon_code=None):
        """Push a status update to Magento (through the middleware) and advance
        the local workflow. Nothing is written if the push fails."""
        self.ensure_one()
        if not self.magento_rma_id:
            raise UserError(self.env._("This RMA has no Magento ID to update."))
        payload = {
            'status': status,
            'admin_message': admin_message or None,
            'resolution': resolution or None,
            'credit_amount': credit_amount or None,
            'coupon_code': coupon_code or None,
            'odoo_reference': self.odoo_reference or None,
        }
        self.env['artaza.magento.connector'].call(
            'POST', 'rma/%s/status' % self.magento_rma_id, payload,
        )
        vals = {'state': status, 'magento_status': status}
        if resolution:
            vals['resolution'] = resolution
        self.write(vals)
        self.message_post(body=self.env._("Pushed to Magento: %s", status))

    def action_accept(self):
        self.ensure_one()
        self._push_status('accepted', admin_message=self.admin_message)

    def action_reject(self):
        self.ensure_one()
        if not self.admin_message:
            raise UserError(self.env._(
                "Fill in 'Message to customer' with the rejection reason before "
                "rejecting. The customer sees it in Magento."
            ))
        self._push_status('rejected', admin_message=self.admin_message)

    def action_receive(self):
        """Goods arrived and are being inspected (operator marks it manually
        after validating the native return picking)."""
        self.ensure_one()
        self._push_status('inspection')

    def action_approve(self):
        self.ensure_one()
        self._push_status('approved')

    def action_fraud(self):
        self.ensure_one()
        self._push_status('fraud', admin_message=self.admin_message)

    def action_resolve_return(self):
        self.ensure_one()
        self._push_status('returned', admin_message=self.admin_message)

    def action_resolve_hold(self):
        self.ensure_one()
        self._push_status('held', admin_message=self.admin_message)

    def action_resolve_exchange(self):
        self.ensure_one()
        self._push_status('resolved_exchange', resolution='exchange',
                          admin_message=self.admin_message)

    def action_resolve_credit(self):
        """Resolve as credit. The credit note is made with native Odoo tools;
        here the operator enters its amount and it is pushed to Magento."""
        self.ensure_one()
        if self.credit_amount <= 0:
            raise UserError(self.env._(
                "Enter the 'Credit Amount' (from the credit note) before "
                "resolving as credit."
            ))
        self._push_status('resolved_credit', resolution='credit',
                          credit_amount=self.credit_amount,
                          coupon_code=self.coupon_code,
                          admin_message=self.admin_message)

    # ── Replacement shipment (Escenario 3: recambio) ───────────
    def action_create_replacement_delivery(self):
        """Create a delivery for the replacement product, pre-filled from the RMA
        (customer + lines). The operator only confirms/validates it. Available
        once the RMA is resolved as exchange."""
        self.ensure_one()
        if self.state != 'resolved_exchange':
            raise UserError(self.env._(
                "The replacement delivery is available once the RMA is resolved as exchange."
            ))
        partner = self.partner_id or self.sale_order_id.partner_id
        if not partner:
            raise UserError(self.env._("This RMA has no customer to deliver to."))
        lines = self.line_ids.filtered('product_id')
        if not lines:
            raise UserError(self.env._("The RMA has no product matched in Odoo to ship."))

        warehouse = self.sale_order_id.warehouse_id or self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1,
        )
        picking_type = warehouse.out_type_id
        if not picking_type:
            raise UserError(self.env._("No delivery operation type is configured."))
        src = picking_type.default_location_src_id or warehouse.lot_stock_id
        dest = partner.property_stock_customer

        moves = [(0, 0, {
            'name': line.name or line.product_id.display_name,
            'product_id': line.product_id.id,
            'product_uom_qty': line.qty_requested or 1.0,
            'product_uom': line.product_id.uom_id.id,
            'location_id': src.id,
            'location_dest_id': dest.id,
        }) for line in lines]

        picking = self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': partner.id,
            'location_id': src.id,
            'location_dest_id': dest.id,
            'origin': self.env._("Replacement %s", self.magento_increment_id),
            'move_ids': moves,
        })
        picking.action_confirm()
        self.message_post(body=self.env._("Replacement delivery created: %s", picking.name))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Replacement delivery"),
            'res_model': 'stock.picking',
            'res_id': picking.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ── Smart button: open the linked sales order ──────────────
    def action_view_sale_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class MagentoRmaLine(models.Model):
    _name = 'magento.rma.line'
    _description = 'Magento RMA line'

    rma_id = fields.Many2one(
        'magento.rma', string="RMA", required=True, ondelete='cascade', index=True,
    )
    sku = fields.Char(string="SKU", readonly=True)
    product_id = fields.Many2one('product.product', string="Product", readonly=True)
    name = fields.Char(string="Description", readonly=True)
    qty_requested = fields.Float(string="Qty to return", readonly=True)
    magento_order_item_id = fields.Integer(string="Magento Order Item ID", readonly=True)
