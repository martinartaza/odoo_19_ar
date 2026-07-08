import logging
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

CURSOR_PARAM = 'artaza_magento_connect.orders_cursor'
OFFLINE_PARAM = 'artaza_magento_connect.offline_methods'
DEFAULT_CURSOR = '2000-01-01 00:00:00'
PULL_STATES = 'processing,complete,pending'
PAGE_SIZE = 50


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    magento_order_id = fields.Char(
        string="ID orden Magento", index=True, copy=False, readonly=True,
    )
    magento_order_state = fields.Char(
        string="Estado en Magento", copy=False, readonly=True,
    )
    magento_order_entity_id = fields.Integer(
        string="Entity ID Magento", copy=False, readonly=True,
    )
    magento_order_total = fields.Monetary(
        string="Total original Magento", copy=False, readonly=True,
        help="Grand total de la orden en Magento al absorberla; base para el ajuste.",
    )
    magento_adjustment_reason = fields.Char(
        string="Motivo del ajuste (Magento)", copy=False,
        help="Se muestra al cliente en su orden de Magento junto al total acordado.",
    )

    _sql_constraints = [
        ('magento_order_id_uniq', 'unique(magento_order_id)',
         "Ya existe una orden de venta con ese ID de Magento."),
    ]

    # ── Cron: pull de órdenes desde Magento ────────────────────
    @api.model
    def _cron_magento_pull_orders(self):
        """Trae órdenes nuevas/actualizadas de Magento (pull con cursor)."""
        icp = self.env['ir.config_parameter'].sudo()
        offline_methods = [
            m.strip()
            for m in (icp.get_param(OFFLINE_PARAM) or 'banktransfer').split(',')
            if m.strip()
        ]
        connector = self.env['artaza.magento.connector']

        for _page in range(1000):  # guarda contra loop infinito
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
                    self._magento_absorb_order(order, offline_methods)
                except Exception as exc:  # noqa: BLE001 - loguear y seguir con la próxima
                    _logger.warning(
                        "Orden Magento %s falló: %s", order.get('increment_id'), exc,
                    )
                # avanzar el cursor aun si una orden falló (create-once idempotente)
                if order.get('updated_at'):
                    icp.set_param(CURSOR_PARAM, order['updated_at'])

            if len(orders) < PAGE_SIZE:
                break

    # ── Absorción de una orden ─────────────────────────────────
    @api.model
    def _magento_absorb_order(self, order, offline_methods):
        state = order.get('state')
        method = order.get('payment_method')
        paid = state in ('processing', 'complete')
        transfer_pending = state == 'pending' and method in offline_methods
        if not (paid or transfer_pending):
            return  # online en curso / no absorbible

        # create-once: si ya existe, Odoo manda y no la pisamos
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
                    "Orden %s: SKU %s no existe en Odoo, línea salteada",
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

        # Odoo es dueño del monto: forzar el precio de Magento (evita que la
        # pricelist recompute el price_unit al crear la línea).
        for line, price in zip(so.order_line, prices):
            if line.price_unit != price:
                line.price_unit = price

        if paid:
            so.action_confirm()  # pagada → orden de venta; transferencia → queda borrador
        return so

    # ── Push del ajuste negociado a Magento (display-only) ─────
    def action_magento_push_negotiation(self):
        """Envía a Magento el ajuste/total acordado para mostrarlo al cliente.

        No toca los balances de Magento: escribe campos informativos. El ajuste
        es el delta contra el total original de la orden en Magento.
        """
        self.ensure_one()
        if not self.magento_order_entity_id:
            raise UserError(self.env._("Esta orden no proviene de Magento."))

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
                'title': self.env._("Ajuste enviado a Magento"),
                'message': self.env._("El cliente verá el total acordado en su orden."),
                'sticky': False,
            },
        }

    # ── Upsert del cliente (por email) ─────────────────────────
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
        ])).strip() or email or 'Cliente Magento'

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
