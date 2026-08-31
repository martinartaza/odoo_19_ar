import logging
import re
import secrets
from datetime import date

from odoo import api, fields, models
from odoo.exceptions import UserError

from .magento_normalize import normalize_rma

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

# Unambiguous alphabet (no 0/o/1/l/i) for the random suffix of a coupon code.
_CODE_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'


def _email_slug(email):
    """Local part of the email, reduced to [a-z0-9._-].

    Dots survive (juan.perez stays juan.perez); '+', spaces and unicode do not.
    The point is a coupon an operator can *search for* in the Magento admin.
    """
    local = (email or '').split('@', 1)[0].lower()
    slug = re.sub(r'[^a-z0-9._-]', '', local).strip('._-')
    return slug[:30] or 'cliente'


def _order_short(order_increment_id):
    """000000031 -> 31. Non-numeric ids are kept, stripped of punctuation."""
    value = (order_increment_id or '').strip()
    if value.isdigit():
        return str(int(value))
    return re.sub(r'[^A-Za-z0-9]', '', value) or '0'


def _random_suffix(length=6):
    return ''.join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


class MagentoRma(models.Model):
    """Return request produced in Magento (Artaza_Rma), pulled into Odoo.

    This is a thin "inbox": Magento is where the customer opens the return and
    Odoo has no native place for a web return request, so we land it here. The
    real work (return picking, credit note) is done with native Odoo tools; the
    decision is pushed back later. Create-once by ``magento_increment_id``:
    Odoo owns the record after import (integration_v3.md §7.4).
    """
    _name = 'artaza.magento.rma'
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
    currency_id = fields.Many2one(
        'res.currency', string="Currency", compute='_compute_currency_id', readonly=True,
    )
    refund_amount_total = fields.Monetary(
        string="Total to refund (ref.)", compute='_compute_refund_amount_total',
        currency_field='currency_id',
        help="Reference amount to refund: the paid price (tax incl., from the Odoo "
             "invoice) of the returned lines. NOT the order total. The credit note "
             "reverses this; edit Credit Amount below for a partial refund.",
    )
    credit_amount = fields.Float(
        string="Credit Amount", copy=False, compute='_compute_credit_amount',
        store=True, readonly=False,
        help="Amount to refund with the credit note. Pre-filled with the returned "
             "lines' paid total; edit it for a partial refund.",
    )
    coupon_code = fields.Char(string="Coupon Code", copy=False)
    odoo_reference = fields.Char(
        string="Odoo Reference", copy=False,
        help="Traceability reference sent to Magento (e.g. the credit note number).",
    )
    magento_created_at = fields.Char(string="Requested At", copy=False, readonly=True)
    magento_updated_at = fields.Char(string="Updated At", copy=False, readonly=True)
    line_ids = fields.One2many(
        'artaza.magento.rma.line', 'rma_id', string="Lines", readonly=True,
    )

    _magento_increment_id_uniq = models.Constraint(
        'unique(magento_increment_id)',
        "A Magento RMA with that number already exists.",
    )

    # ── Refund reference amounts (from the Odoo invoice) ───────
    @api.depends('sale_order_id', 'sale_order_id.currency_id')
    def _compute_currency_id(self):
        for rma in self:
            rma.currency_id = rma.sale_order_id.currency_id or rma.env.company.currency_id

    @api.depends('line_ids.price_subtotal')
    def _compute_refund_amount_total(self):
        for rma in self:
            rma.refund_amount_total = sum(rma.line_ids.mapped('price_subtotal'))

    @api.depends('refund_amount_total')
    def _compute_credit_amount(self):
        """Pre-fill the credit amount with the reference total, but leave it
        editable (partial refunds). Never clobber an operator-entered value."""
        for rma in self:
            if not rma.credit_amount:
                rma.credit_amount = rma.refund_amount_total

    # ── Cron: pull RMAs from Magento ───────────────────────────
    @api.model
    def _cron_magento_pull_rmas(self):
        """Pull new/updated RMAs from Magento (cursor-based). Returns the count
        of newly created records (used by the manual "Import now" button)."""
        icp = self.env['ir.config_parameter'].sudo()
        client = self.env['artaza.magento.client']
        created = 0

        log = self.env['artaza.magento.sync.log']
        for _page in range(1000):  # guard against an infinite loop
            cursor = icp.get_param(CURSOR_PARAM) or DEFAULT_CURSOR
            with log.track('rma_pull', 'in', 'cron') as tracker:
                tracker.request_payload = 'updated_at >= %s | page_size=%s' % (
                    cursor, PAGE_SIZE)
                rmas = [normalize_rma(raw)
                        for raw in client.fetch_rmas(cursor, PAGE_SIZE)]
                if not rmas:
                    break

                for rma in rmas:
                    increment_id = rma.get('increment_id')
                    try:
                        if self._magento_absorb_rma(rma):
                            created += 1
                        tracker.ok(increment_id)
                    except Exception as exc:  # noqa: BLE001 - continue with the next one
                        # The cursor moves past it, so an unrecorded failure is
                        # a return that silently never arrives.
                        _logger.warning(
                            "Magento RMA %s failed: %s", increment_id, exc,
                        )
                        tracker.fail(increment_id, exc)
                    # advance the cursor even if one failed (create-once is idempotent)
                    if rma.get('updated_at'):
                        icp.set_param(CURSOR_PARAM, rma['updated_at'])

            if len(rmas) < PAGE_SIZE:
                break

        return created

    # ── Manual import of one RMA by number (bypasses the cursor) ──
    @api.model
    def _magento_import_one(self, increment_id):
        """Import a single Magento return by its number, ignoring the cursor.

        Same contract as ``sale.order._magento_import_one`` on purpose: the sync
        history replays a failed import through one code path, whichever kind it
        was. Returns {status: exists|imported|not_found|error, record?, message?}.
        """
        increment_id = (increment_id or '').strip()
        if not increment_id:
            return {'status': 'error', 'message': self.env._("Enter a return number.")}

        existing = self.search([('magento_increment_id', '=', increment_id)], limit=1)
        if existing:
            return {'status': 'exists', 'record': existing}

        try:
            raw = self.env['artaza.magento.client'].fetch_rma_by_increment(increment_id)
        except Exception as exc:  # noqa: BLE001 - surface the Magento message
            return {'status': 'error', 'message': str(exc)}

        rma = normalize_rma(raw) if raw else None
        if not rma:
            return {'status': 'not_found',
                    'message': self.env._("Magento returned no return %s.", increment_id)}

        try:
            record = self._magento_absorb_rma(rma)
        except Exception as exc:  # noqa: BLE001 - surface any absorption error
            return {'status': 'error', 'message': str(exc)}
        return {'status': 'imported', 'record': record}

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
        """Push a status update to Magento and advance
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
        log = self.env['artaza.magento.sync.log']
        with log.track('rma_status', 'out', 'ui') as tracker:
            tracker.attempt(self.magento_increment_id)
            self.env['artaza.magento.client'].write_rma_status(
                self.magento_rma_id,
                status,
                admin_message=payload['admin_message'],
                resolution=payload['resolution'],
                credit_amount=payload['credit_amount'],
                coupon_code=payload['coupon_code'],
                odoo_reference=payload['odoo_reference'],
            )
            tracker.ok(self.magento_increment_id)
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
                "rejecting. The customer sees it in Magento.",
            ))
        self._push_status('rejected', admin_message=self.admin_message)

    def action_receive(self):
        """Goods arrived and are being inspected (operator marks it manually
        after validating the native return picking)."""
        self.ensure_one()
        self._push_status('inspection')

    def action_approve(self):
        """Open the restock-decision wizard: the product passed inspection, but
        whether it re-enters sellable stock depends on the operator (a wrong-color
        item is resellable; a factory-defective one is not)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._("Approve product"),
            'res_model': 'artaza.magento.rma.approve.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_rma_id': self.id},
        }

    def _approve_with_restock(self, restock):
        """Approve + auto-receive the returned goods per the wizard decision:
        into sellable stock (`sellable`) or into scrap (`scrap`, damaged unit)."""
        self.ensure_one()
        if restock in ('sellable', 'scrap'):
            self._create_return_picking(to_scrap=(restock == 'scrap'))
        self._push_status('approved')

    def _create_return_picking(self, to_scrap=False):
        """Receive the returned product into stock (auto-validated). Destination:
        the warehouse's sellable stock, or the scrap location when it's damaged."""
        self.ensure_one()
        partner = self.partner_id or self.sale_order_id.partner_id
        lines = self.line_ids.filtered('product_id')
        if not partner or not lines:
            return
        warehouse = self.sale_order_id.warehouse_id or self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1,
        )
        picking_type = warehouse.in_type_id
        if not picking_type:
            raise UserError(self.env._("No receipt operation type is configured."))
        src = partner.property_stock_customer
        if to_scrap:
            # Odoo's own scrap destination: the company's inventory-usage location
            # with the lowest id (see stock.scrap._compute_scrap_location_id).
            dest = self.env['stock.location'].search([
                ('company_id', 'in', [self.env.company.id, False]),
                ('usage', '=', 'inventory'),
            ], order='id', limit=1) or picking_type.default_location_dest_id
        else:
            dest = picking_type.default_location_dest_id or warehouse.lot_stock_id

        moves = [(0, 0, {
            'product_id': line.product_id.id,
            'description_picking': line.name or line.product_id.display_name,
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
            'origin': self.env._("RMA return %s", self.magento_increment_id),
            'move_ids': moves,
        })
        picking.action_confirm()
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        picking.with_context(skip_backorder=True, skip_sms=True)._action_done()
        self.message_post(body=self.env._(
            "Returned product received: %(pick)s → %(loc)s",
            pick=picking.name, loc=dest.display_name,
        ))

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
        """Resolve as credit — in one action:
          1. create + post the **credit note** that reverses the invoice
             (fiscal backing), recording its number in odoo_reference;
          2. generate the Magento **coupon** for the credit amount (how the
             customer redeems it) and push the resolution to Magento.

        Both steps are idempotent (NC skipped if odoo_reference is set; coupon is
        idempotent by RMA number), so a retry never duplicates. Guarded by a
        confirmation dialog on the button."""
        self.ensure_one()
        if self.credit_amount <= 0:
            raise UserError(self.env._(
                "Enter the 'Credit Amount' before resolving as credit.",
            ))
        if not self.odoo_reference:
            self._create_credit_note()
        if not self.coupon_code:
            self._generate_coupon()
        self._push_status('resolved_credit', resolution='credit',
                          credit_amount=self.credit_amount,
                          coupon_code=self.coupon_code,
                          admin_message=self.admin_message)

    def _create_credit_note(self):
        """Create + post the credit note that reverses the order's invoice,
        limited to the RETURNED lines/quantities (**partial-return aware**): if the
        customer bought 2 and returns 1, the NC is only for that 1.

        Built from the native reversal (so l10n_ar sets the right NC document type,
        taxes and fiscal position), then trimmed to what the RMA actually returns.
        Records the NC number in odoo_reference. Shipping is not an RMA line, so it
        is not refunded — the NC total matches the returned products (= credit)."""
        self.ensure_one()
        order = self.sale_order_id
        invoice = order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted',
        )[:1]
        if not invoice:
            raise UserError(self.env._(
                "This order isn't invoiced yet, so there is no invoice for the credit "
                "note to reverse. Invoice the sale order first (Create Invoice → "
                "Confirm), then resolve this RMA as credit.",
            ))
        # Returned quantity per product (0 = product not in this return).
        returned = {}
        for line in self.line_ids.filtered('product_id'):
            returned[line.product_id.id] = (
                returned.get(line.product_id.id, 0.0) + (line.qty_requested or 0.0)
            )

        # Use the native reversal WIZARD (not _reverse_moves directly): the
        # l10n_latam extension computes the correct NC document type (NC B for a
        # Factura B) and passes it to the reversal. Calling _reverse_moves directly
        # would copy the invoice's document type → "can't use type invoice on a
        # refund" error.
        reversal = self.env['account.move.reversal'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({
            'move_ids': [(6, 0, invoice.ids)],
            'journal_id': invoice.journal_id.id,
            'reason': self.env._("Credit note · RMA %s", self.magento_increment_id),
            'date': fields.Date.context_today(self),
            'company_id': invoice.company_id.id,
        })
        reversal.reverse_moves()  # draft NC with the right document type
        credit_note = reversal.new_move_ids
        if not credit_note:
            raise UserError(self.env._("The credit note could not be created."))
        credit_note.ensure_one()
        # Keep only the returned lines, at the returned quantities. (In Odoo 19
        # product lines carry display_type='product'; only sections/notes are skipped.)
        to_unlink = credit_note.invoice_line_ids.browse()
        for cn_line in credit_note.invoice_line_ids:
            if cn_line.display_type in ('line_section', 'line_note') or not cn_line.product_id:
                continue
            want = returned.get(cn_line.product_id.id, 0.0)
            if want <= 0:
                to_unlink |= cn_line
            elif cn_line.quantity > want:
                cn_line.quantity = want
        to_unlink.unlink()

        if not credit_note.invoice_line_ids.filtered(
            lambda line: line.product_id and line.display_type not in ('line_section', 'line_note'),
        ):
            credit_note.unlink()
            raise UserError(self.env._(
                "No returned line matches the invoice; cannot build the credit note.",
            ))

        credit_note.action_post()
        self.odoo_reference = credit_note.name
        self.message_post(body=self.env._("Credit note created: %s", credit_note.name))

    def _generate_coupon(self):
        """Create the single-use Magento coupon backing this RMA's credit.

        **Idempotency lives on the record:** the RMA itself holds
        the code. If `coupon_code` is already set, the coupon exists and this is
        a retry — return instead of minting a second one. That is the same
        guarantee the `coupons` table gave, using the record that was already
        the natural key (the RMA number).
        """
        self.ensure_one()
        if self.coupon_code:
            return  # already issued — a retry must never mint a second coupon
        email = self.customer_email or self.partner_id.email
        if not email:
            raise UserError(self.env._(
                "The RMA has no customer email to issue the coupon to.",
            ))

        client = self.env['artaza.magento.client']
        website_id = self._magento_coupon_website(client)
        rule_name = '%s-%s' % (
            _email_slug(email), _order_short(self.magento_order_increment_id))
        code = '%s-%s' % (rule_name, _random_suffix())

        log = self.env['artaza.magento.sync.log']
        with log.track('coupon', 'out', 'ui') as tracker:
            tracker.attempt(self.magento_increment_id)
            rule_id = client.create_cart_price_rule({
                'name': rule_name,
                'description': 'Store credit (rma_credit) · %s' % self.magento_increment_id,
                'website_ids': [website_id],
                # Every group plus guest: the customer may redeem it either way.
                'customer_group_ids': client.fetch_customer_group_ids(),
                'coupon_type': 'SPECIFIC_COUPON',
                'use_auto_generation': False,
                'uses_per_coupon': 1,
                'uses_per_customer': 1,
                'is_active': True,
                'stop_rules_processing': True,
                'is_advanced': True,
                'simple_action': 'cart_fixed',
                'discount_amount': self.credit_amount,
                'discount_step': 0,
                'apply_to_shipping': False,
                'from_date': date.today().isoformat(),
                'to_date': None,
            })
            client.create_specific_coupon(rule_id, code)
            tracker.ok(self.magento_increment_id)

        # Written immediately: if anything later in the flow fails, the retry
        # must find the code and not issue a second coupon.
        self.coupon_code = code
        self.message_post(body=self.env._("Discount coupon generated: %s", code))

    def _magento_coupon_website(self, client):
        """Website the coupon is valid in.

        Resolved from the ORDER's store view, so the credit is redeemable in the
        same store the purchase was made in — without Odoo having to know any
        Magento store code. Falls back to the first active store view.
        """
        self.ensure_one()
        views = client.fetch_store_views()
        if self.magento_order_entity_id:
            order = client.get_order(self.magento_order_entity_id)
            store_id = order.get('store_id')
            if store_id is not None:
                for view in views:
                    if int(view.get('id') or -1) == int(store_id) and view.get('website_id'):
                        return int(view['website_id'])
        for view in views:
            if view.get('website_id'):
                return int(view['website_id'])
        raise UserError(self.env._(
            "Could not work out which Magento website this coupon belongs to.",
        ))

    # ── Outgoing delivery to the customer ──────────────────────
    # Reused by two flows that both ship a product to the customer:
    #   • resolved_exchange (Escenario 3): the replacement product.
    #   • returned (Escenario 4): the product handed back after a fraud call.
    def action_create_replacement_delivery(self):
        """Create a delivery to the customer, pre-filled from the RMA (customer +
        lines). The operator only confirms/validates it. Available once the RMA is
        resolved as exchange (replacement) or as returned to customer."""
        self.ensure_one()
        if self.state not in ('resolved_exchange', 'returned'):
            raise UserError(self.env._(
                "The delivery is available once the RMA is resolved as exchange "
                "or returned to the customer.",
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

        if self.state == 'returned':
            origin = self.env._("Return to customer %s", self.magento_increment_id)
        else:
            origin = self.env._("Replacement %s", self.magento_increment_id)

        moves = [(0, 0, {
            'product_id': line.product_id.id,
            'description_picking': line.name or line.product_id.display_name,
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
            'origin': origin,
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
    _name = 'artaza.magento.rma.line'
    _description = 'Magento RMA line'

    rma_id = fields.Many2one(
        'artaza.magento.rma', string="RMA", required=True, ondelete='cascade', index=True,
    )
    sku = fields.Char(string="SKU", readonly=True)
    product_id = fields.Many2one('product.product', string="Product", readonly=True)
    name = fields.Char(string="Description", readonly=True)
    qty_requested = fields.Float(string="Qty to return", readonly=True)
    magento_order_item_id = fields.Integer(string="Magento Order Item ID", readonly=True)
    currency_id = fields.Many2one(related='rma_id.currency_id', readonly=True)
    price_unit = fields.Monetary(
        string="Unit price paid", compute='_compute_amounts',
        currency_field='currency_id',
        help="Unit price the customer actually paid (tax included), taken from the "
             "posted Odoo invoice. Falls back to the sales order line.",
    )
    price_subtotal = fields.Monetary(
        string="Subtotal", compute='_compute_amounts', currency_field='currency_id',
        help="price paid x qty to return.",
    )

    @api.depends('product_id', 'qty_requested', 'rma_id.sale_order_id')
    def _compute_amounts(self):
        for line in self:
            unit = line._paid_unit_price()
            line.price_unit = unit
            line.price_subtotal = unit * (line.qty_requested or 0.0)

    def _paid_unit_price(self):
        """Per-unit gross price (tax incl.) the customer paid for this product.

        Source of truth = the **posted Odoo invoice** (fiscal master), so it
        already reflects the customer-group discount and any negotiated total.
        Falls back to the sales order line, then 0. Magento never computes money.
        """
        self.ensure_one()
        order = self.rma_id.sale_order_id
        if not self.product_id or not order:
            return 0.0
        for move in order.invoice_ids.filtered(
            lambda m: m.move_type == 'out_invoice' and m.state == 'posted',
        ):
            inv_line = move.invoice_line_ids.filtered(
                lambda line: line.product_id == self.product_id and not line.display_type,
            )[:1]
            if inv_line and inv_line.quantity:
                return inv_line.price_total / inv_line.quantity
        so_line = order.order_line.filtered(
            lambda line: line.product_id == self.product_id and not line.display_type,
        )[:1]
        if so_line and so_line.product_uom_qty:
            return so_line.price_total / so_line.product_uom_qty
        return 0.0
