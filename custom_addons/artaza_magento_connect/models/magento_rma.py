import logging
from urllib.parse import quote

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

CURSOR_PARAM = 'artaza_magento_connect.rmas_cursor'
DEFAULT_CURSOR = '2000-01-01 00:00:00'
PAGE_SIZE = 50


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
    resolution = fields.Char(string="Resolution", copy=False, readonly=True)
    credit_amount = fields.Float(string="Credit Amount", copy=False, readonly=True)
    coupon_code = fields.Char(string="Coupon Code", copy=False, readonly=True)
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
