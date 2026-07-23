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

        connector = self.env['artaza.magento.connector']
        try:
            result = connector.call('GET', 'orders/%s' % quote(increment_id))
        except Exception as exc:  # noqa: BLE001 - surface the middleware/Magento message
            return {'status': 'error', 'message': str(exc)}

        order = result.get('order')
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
        for line, price in zip(so.order_line, prices):
            if line.price_unit != price:
                line.price_unit = price

        # The shipping line needs an IVA tax (l10n_ar requires exactly one per
        # line, or the invoice won't post). See _magento_shipping_tax for how the
        # rate is chosen (the freight rate is a fiscal decision, not the product's).
        shipping_line = so.order_line.filtered(
            lambda l: l.product_id.default_code == 'MAGENTO_SHIPPING'
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
                "adjustment. The customer will see it next to the agreed total."
            ))

        adjustment = self.amount_total - (self.magento_order_total or 0.0)
        self.env['artaza.magento.connector'].call('POST', 'negotiation', {
            'order_id': self.magento_order_entity_id,
            'adjustment_amount': adjustment,
            'adjustment_reason': self.magento_adjustment_reason or None,
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

    # ── Open the "adjust total" wizard (modal) ─────────────────
    def action_open_set_total_wizard(self):
        """Open a modal to type the desired total; it back-calculates the line
        discount/surcharge and fills the Magento adjustment reason."""
        self.ensure_one()
        return {
            'name': self.env._("Adjust total"),
            'type': 'ir.actions.act_window',
            'res_model': 'artaza.sale.total.wizard',
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
           21%, its own service rate), **not** the product's alícuota — so a
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
            lambda l: l.product_id.default_code != 'MAGENTO_SHIPPING'
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

    # ── AFIP fiscal mapping (Magento condition → Odoo) ─────────
    @api.model
    def _magento_afip_data(self, condition):
        """Map the Magento fiscal condition to (responsibility, id_type).

        Defaults to **Consumidor Final** (the B2C case) when the condition is
        empty/unknown, so a customer imported without fiscal data can still be
        invoiced (Factura B). Returns (False, False) if l10n_ar is not installed.
        """
        if 'l10n_ar.afip.responsibility.type' not in self.env:
            return False, False
        # Magento code → AFIP responsibility code (5 = Consumidor Final, default)
        resp_code = {
            'consumidor_final': '5',
            'responsable_inscripto': '1',
            'monotributo': '6',
            'exento': '4',
        }.get(condition or '', '5')
        responsibility = self.env['l10n_ar.afip.responsibility.type'].search(
            [('code', '=', resp_code)], limit=1,
        )
        # Consumidor Final → DNI; the rest are businesses → CUIT.
        id_name = 'DNI' if resp_code == '5' else 'CUIT'
        id_type = self.env['l10n_latam.identification.type'].search(
            [('name', '=', id_name), ('country_id.code', '=', 'AR')], limit=1,
        )
        return responsibility, id_type

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
        responsibility, id_type = self._magento_afip_data(condition)

        vals = {}
        if responsibility and partner.l10n_ar_afip_responsibility_type_id != responsibility:
            vals['l10n_ar_afip_responsibility_type_id'] = responsibility.id
        if id_type and partner.l10n_latam_identification_type_id != id_type:
            vals['l10n_latam_identification_type_id'] = id_type.id
        if vat and partner.vat != vat:
            vals['vat'] = vat
        if not vals:
            return
        try:
            partner.write(vals)
            partner.message_post(body=self.env._(
                "Fiscal condition updated from Magento order %(order)s: %(cond)s",
                order=order.get('increment_id'),
                cond=responsibility.name if responsibility else condition,
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
        # Fiscal condition (AFIP) so the invoice picks the right document type.
        responsibility, id_type = self._magento_afip_data(customer.get('afip_responsibility'))
        if responsibility:
            vals['l10n_ar_afip_responsibility_type_id'] = responsibility.id
        if id_type:
            vals['l10n_latam_identification_type_id'] = id_type.id
        return Partner.create(vals)
