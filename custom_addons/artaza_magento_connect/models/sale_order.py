import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .magento_normalize import normalize_order

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

    _magento_order_id_uniq = models.Constraint(
        'unique(magento_order_id)',
        "A sales order with that Magento ID already exists.",
    )

    # ── Cron: pull orders from Magento ─────────────────────────
    @api.model
    def _cron_magento_pull_orders(self):
        """Pull new/updated orders from Magento (cursor-based)."""
        icp = self.env['ir.config_parameter'].sudo()
        processing_methods = _split_methods(icp.get_param(PROCESSING_METHODS_PARAM))
        pending_methods = _split_methods(icp.get_param(PENDING_METHODS_PARAM))
        client = self.env['artaza.magento.client']

        log = self.env['artaza.magento.sync.log']
        for _page in range(1000):  # guard against an infinite loop
            cursor = icp.get_param(CURSOR_PARAM) or DEFAULT_CURSOR
            # One history entry per page: the fetch itself can fail (auth, network),
            # and each order inside can fail on its own without stopping the rest.
            with log.track('order_pull', 'in', 'cron') as tracker:
                tracker.request_payload = 'updated_at >= %s | states=%s | page_size=%s' % (
                    cursor, PULL_STATES, PAGE_SIZE)
                raw_orders = client.fetch_orders(cursor, PULL_STATES, PAGE_SIZE)
                orders = [normalize_order(raw) for raw in raw_orders]
                if not orders:
                    break

                for order in orders:
                    increment_id = order.get('increment_id')
                    try:
                        self._magento_absorb_order(
                            order, processing_methods, pending_methods)
                        tracker.ok(increment_id)
                    except Exception as exc:  # noqa: BLE001 - continue with the next one
                        # The cursor moves past a failed order (create-once is
                        # idempotent), so without this line it would be lost for good.
                        _logger.warning(
                            "Magento order %s failed: %s", increment_id, exc,
                        )
                        tracker.fail(increment_id, exc)
                    # advance the cursor even if an order failed
                    if order.get('updated_at'):
                        icp.set_param(CURSOR_PARAM, order['updated_at'])

            if len(orders) < PAGE_SIZE:
                break

    # ── Manual import of one order by number (bypasses the cursor) ──
    @api.model
    def _magento_import_one(self, increment_id):
        """Import a single Magento order by its number, ignoring the cursor and
        the payment-method gate. Returns a dict the wizard renders:
        {status: exists|imported|not_found|error, order?, message?}."""
        increment_id = (increment_id or '').strip()
        if not increment_id:
            return {'status': 'error', 'message': self.env._("Enter an order number.")}

        existing = self.search([('magento_order_id', '=', increment_id)], limit=1)
        if existing:
            return {'status': 'exists', 'order': existing}

        try:
            raw = self.env['artaza.magento.client'].fetch_order_by_increment(increment_id)
        except Exception as exc:  # noqa: BLE001 - surface the Magento message
            return {'status': 'error', 'message': str(exc)}

        order = normalize_order(raw) if raw else None
        if not order:
            return {'status': 'not_found',
                    'message': self.env._("Magento returned no order %s.", increment_id)}

        icp = self.env['ir.config_parameter'].sudo()
        processing_methods = _split_methods(icp.get_param(PROCESSING_METHODS_PARAM))
        pending_methods = _split_methods(icp.get_param(PENDING_METHODS_PARAM))
        try:
            so = self._magento_absorb_order(order, processing_methods, pending_methods, force=True)
        except Exception as exc:  # noqa: BLE001 - surface any absorption error
            return {'status': 'error', 'message': str(exc)}
        return {'status': 'imported', 'order': so}

    # ── Absorb a single order ──────────────────────────────────
    @api.model
    def _magento_absorb_order(self, order, processing_methods, pending_methods, force=False):
        state = order.get('state')
        method = order.get('payment_method')
        # Magento state: 'processing'/'complete' = paid; 'new' (status pending) = placed, unpaid.
        # Two method whitelists: paid ones import as confirmed sales (no price change); pending
        # ones import as editable quotations (negotiable; price can be pushed back to Magento).
        paid = state in ('processing', 'complete') and method in processing_methods
        negotiable = state == 'new' and method in pending_methods
        # force=True (manual import) brings the order in regardless of method/state.
        if not (paid or negotiable) and not force:
            return None  # unlisted method / online in-flight (pending_payment) → not absorbable

        # create-once: if it already exists, Odoo owns it and we do not overwrite it
        existing = self.search([('magento_order_id', '=', order['increment_id'])], limit=1)
        if existing:
            return existing

        # Resolve every SKU BEFORE writing anything. An order that comes in SHORT
        # is worse than one that does not come in at all: its total stops matching
        # Magento's, and skipping the line only left a _logger.warning — the very
        # blind spot v4 exists to remove. Refusing the order sends it to the sync
        # history instead, where "Re-import" replays it once the product exists.
        # Checked before the partner upsert so a rejected order leaves no trace.
        items = order.get('items', [])
        products_by_sku = {}
        missing_skus = []
        for item in items:
            product = self.env['product.product'].search(
                [('default_code', '=', item['sku'])], limit=1,
            )
            if product:
                products_by_sku[item['sku']] = product
            elif item['sku'] not in missing_skus:
                missing_skus.append(item['sku'])
        if missing_skus:
            raise UserError(self.env._(
                "Order %(order)s was not imported: no product in Odoo has the "
                "Internal Reference %(skus)s. Create it (or fix the reference) "
                "and re-import the order from the sync history.",
                order=order['increment_id'], skus=", ".join(missing_skus),
            ))

        partner = self._magento_upsert_partner(order)

        line_commands = []
        prices = []
        for item in items:
            product = products_by_sku[item['sku']]
            line_commands.append((0, 0, {
                'product_id': product.id,
                'product_uom_qty': item.get('qty') or 0.0,
                'price_unit': item.get('price') or 0.0,
                'name': item.get('name') or product.display_name,
            }))
            prices.append(item.get('price') or 0.0)

        # Shipping is a line, not a hidden fee: add it as a service line so the
        # SO total matches Magento's grand_total (products + shipping) and the
        # invoice carries the delivery cost. (Magento sends it tax-included.)
        shipping_amount = order.get('shipping_amount') or 0.0
        if shipping_amount:
            shipping_product = self._magento_shipping_product()
            line_commands.append((0, 0, {
                'product_id': shipping_product.id,
                'product_uom_qty': 1.0,
                'price_unit': shipping_amount,
                'name': self.env._("Shipping"),
            }))
            prices.append(shipping_amount)

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
        #
        # strict=True because the pairing is positional: `prices` is built in
        # lockstep with `line_commands`, but `so.order_line` is whatever create()
        # returned, ordered by sequence. If anything ever adds or reorders a line,
        # a plain zip() would not fail -- it would pair a price with the wrong
        # line, and a wrong amount would reach the invoice with no error anywhere.
        # Raising is the lesser harm.
        for line, price in zip(so.order_line, prices, strict=True):
            if line.price_unit != price:
                line.price_unit = price

        # The shipping line needs an IVA tax (l10n_ar requires exactly one per
        # line, or the invoice won't post). See _magento_shipping_tax for how the
        # rate is chosen (the freight rate is a fiscal decision, not the product's).
        shipping_line = so.order_line.filtered(
            lambda line: line.product_id.default_code == 'MAGENTO_SHIPPING',
        )
        if shipping_line and not shipping_line.tax_ids:
            tax = self._magento_shipping_tax(so)
            if tax:
                shipping_line.tax_ids = [(6, 0, tax.ids)]
                _logger.info(
                    "Order %s: shipping line tax set to %s",
                    order['increment_id'], tax.name,
                )

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
        if not self.magento_adjustment_reason:
            raise UserError(self.env._(
                "Fill in 'Adjustment Reason (Magento)' before sending the "
                "adjustment. The customer will see it next to the agreed total.",
            ))

        adjustment = self.amount_total - (self.magento_order_total or 0.0)
        log = self.env['artaza.magento.sync.log']
        with log.track('negotiation', 'out', 'ui') as tracker:
            tracker.attempt(self.magento_order_id)
            self.env['artaza.magento.client'].write_order_negotiation(
                self.magento_order_entity_id,
                adjustment,
                self.magento_adjustment_reason or None,
                self.amount_total,
            )
            tracker.ok(self.magento_order_id)
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

    # ── Open the "adjust total" wizard (modal) ─────────────────
    def action_open_set_total_wizard(self):
        """Open a modal to type the desired total; it back-calculates the line
        discount/surcharge and fills the Magento adjustment reason."""
        self.ensure_one()
        return {
            'name': self.env._("Adjust total"),
            'type': 'ir.actions.act_window',
            'res_model': 'artaza.magento.order.total.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_order_id': self.id},
        }

    # ── Tax for the shipping line ──────────────────────────────
    @api.model
    def _magento_shipping_tax(self, so):
        """Tax for the shipping line, in priority order:

        1. The **configured shipping tax** (Settings ▸ Magento Connect). This is
           the fiscally correct path: the freight's IVA is a decision (usually
           21%, its own service rate), **not** the product's rate — so a
           10.5% product does NOT force a 10.5% shipping, and mixed-rate orders
           are handled deterministically. Pick the *price-included* sale tax.
        2. Otherwise, the first product's own tax — Magento prices are
           tax-included, so mirroring it keeps the total unchanged (safe default
           for single-rate stores that don't configure a shipping tax).
        3. The company's default sale tax as a last resort.
        """
        icp = self.env['ir.config_parameter'].sudo()
        tax_id = icp.get_param('artaza_magento_connect.shipping_tax_id')
        if tax_id:
            tax = self.env['account.tax'].browse(int(tax_id)).exists()
            if tax:
                return tax
        first_product = so.order_line.filtered(
            lambda line: line.product_id.default_code != 'MAGENTO_SHIPPING',
        )[:1].product_id
        return first_product.taxes_id[:1] or self.env.company.account_sale_tax_id

    # ── Shipping product (get-or-create) ───────────────────────
    @api.model
    def _magento_shipping_product(self):
        """Return the service product used for the Magento shipping line.

        Get-or-create by `default_code='MAGENTO_SHIPPING'`. A service product
        (not stockable) keeps it out of inventory; it exists only to carry the
        shipping cost on the sales order and invoice.
        """
        code = 'MAGENTO_SHIPPING'
        product = self.env['product.product'].search(
            [('default_code', '=', code)], limit=1,
        )
        if not product:
            product = product.create({
                'name': self.env._("Shipping"),
                'default_code': code,
                'type': 'service',
                'list_price': 0.0,
                'sale_ok': True,
                'purchase_ok': False,
                'taxes_id': [(5, 0, 0)],  # no default tax: price is tax-included
            })
        return product

    # ── Fiscal condition (Magento → Odoo) ──────────────────────
    @api.model
    def _magento_fiscal_partner_values(self, condition):
        """Partner fields that carry the customer's fiscal condition.

        **This is the extension point for other localisations.** Everything else
        in the import — addresses, lines, totals, taxes — is country-agnostic;
        only the meaning of `condition` is local. Magento sends a plain code
        (``consumidor_final``, ``responsable_inscripto``…) and each localisation
        decides which partner fields it maps to.

        To support another country, override this method and return that
        country's fields. Returning ``{}`` is always safe: the customer is
        imported without fiscal data and nothing else breaks.

        The implementation below is **Argentina** (``l10n_ar``), and it returns
        ``{}`` when that localisation is not installed.
        """
        if 'l10n_ar.afip.responsibility.type' not in self.env:
            return {}
        # Magento code → AFIP responsibility code (5 = Consumidor Final, default)
        resp_code = {
            'consumidor_final': '5',
            'responsable_inscripto': '1',
            'monotributo': '6',
            'exento': '4',
        }.get(condition or '', '5')
        values = {}
        responsibility = self.env['l10n_ar.afip.responsibility.type'].search(
            [('code', '=', resp_code)], limit=1,
        )
        if responsibility:
            values['l10n_ar_afip_responsibility_type_id'] = responsibility.id
        # Consumidor Final → DNI; the rest are businesses → CUIT.
        id_type = self.env['l10n_latam.identification.type'].search(
            [('name', '=', 'DNI' if resp_code == '5' else 'CUIT'),
             ('country_id.code', '=', 'AR')], limit=1,
        )
        if id_type:
            values['l10n_latam_identification_type_id'] = id_type.id
        return values

    # ── Update the fiscal data of an existing customer ─────────
    @api.model
    def _magento_update_fiscal(self, partner, order):
        """Refresh an existing customer's AFIP data from the latest order.

        Only when the order carries an **explicit** fiscal condition (so an order
        without the field never clobbers a customer already set to RI with a
        default Consumidor Final). Touches only fiscal fields — never name or
        address — and only what actually changed. Never blocks the import.
        """
        customer = order.get('customer') or {}
        condition = customer.get('afip_responsibility')
        if not condition:
            return
        billing = order.get('billing') or {}
        vat = billing.get('vat_id')
        vals = {}
        for field, value in self._magento_fiscal_partner_values(condition).items():
            current = partner[field]
            current = current.id if hasattr(current, 'id') else current
            if current != value:
                vals[field] = value
        if vat and partner.vat != vat:
            vals['vat'] = vat
        if not vals:
            return
        try:
            partner.write(vals)
            partner.message_post(body=self.env._(
                "Fiscal condition updated from Magento order %(order)s: %(cond)s",
                order=order.get('increment_id'),
                cond=condition,
            ))
        except Exception as exc:  # noqa: BLE001 - never block the order import
            _logger.warning(
                "Order %s: could not update fiscal data of %s: %s",
                order.get('increment_id'), partner.email, exc,
            )

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
                # Existing customer: refresh the fiscal condition from THIS order
                # (a customer can move from Consumidor Final to Responsable
                # Inscripto between purchases → next invoice must be an A, not a B).
                self._magento_update_fiscal(partner, order)
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

        vals = {
            'name': name,
            'email': email or False,
            'phone': billing.get('telephone') or False,
            'street': billing.get('street') or False,
            'city': billing.get('city') or False,
            'zip': billing.get('postcode') or False,
            'country_id': country.id or False,
            'state_id': state.id or False,
            'vat': billing.get('vat_id') or False,
        }
        # Fiscal condition, so the invoice picks the right document type.
        vals.update(self._magento_fiscal_partner_values(customer.get('afip_responsibility')))
        return Partner.create(vals)
