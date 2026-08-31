from odoo import api, fields, models

from .artaza_magento_client import (
    PARAM_MAGENTO_SKIP_SSL,
    PARAM_MAGENTO_TOKEN,
    PARAM_MAGENTO_URL,
)
from .artaza_magento_rma import CURSOR_PARAM as RMAS_CURSOR_PARAM
from .artaza_magento_rma import DEFAULT_CURSOR as RMAS_DEFAULT_CURSOR
from .sale_order import CURSOR_PARAM as ORDERS_CURSOR_PARAM
from .sale_order import DEFAULT_CURSOR as ORDERS_DEFAULT_CURSOR

CRON_XMLID = 'artaza_magento_connect.ir_cron_magento_stock_sync'
ORDERS_CRON_XMLID = 'artaza_magento_connect.ir_cron_magento_pull_orders'
RMAS_CRON_XMLID = 'artaza_magento_connect.ir_cron_magento_pull_rmas'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ── Magento connection ─────────────────────────────────────
    magento_base_url = fields.Char(
        string="Magento URL",
        config_parameter=PARAM_MAGENTO_URL,
        help="Base URL of the Magento store, with no trailing slash, "
             "e.g. https://www.mystore.com. The REST paths are added by Odoo.",
    )
    magento_token = fields.Char(
        string="Integration token",
        config_parameter=PARAM_MAGENTO_TOKEN,
        help="Access token of the Magento integration "
             "(System ▸ Extensions ▸ Integrations). Sent as "
             "'Authorization: Bearer <token>'.",
    )
    magento_skip_ssl_verify = fields.Boolean(
        string="Do not verify the SSL certificate",
        config_parameter=PARAM_MAGENTO_SKIP_SSL,
        help="Tick ONLY in local development, when the store is served over "
             "HTTPS with a self-signed certificate. Leave it unticked in "
             "production.",
    )

    # ── Mapping screens (Odoo ⇄ Magento, by name) ──────────────
    # Plain Many2many on the transient record: the rows are the REAL warehouses
    # and taxes, so editing the mapping column inline writes straight to them.
    magento_warehouse_ids = fields.Many2many(
        'stock.warehouse',
        'artaza_magento_config_warehouse_rel', 'config_id', 'warehouse_id',
        string="Warehouses",
    )
    magento_sale_tax_ids = fields.Many2many(
        'account.tax',
        'artaza_magento_config_tax_rel', 'config_id', 'tax_id',
        string="Sale taxes",
    )
    # ── Sync history (v4 objective 2) ──────────────────────────
    magento_log_retention_days = fields.Integer(
        string="Keep successful runs for (days)",
        config_parameter='artaza_magento_connect.log_retention_days',
        default=30,
        help="A daily cron deletes SUCCESSFUL runs older than this. Failures are "
             "never deleted automatically — an old error is usually the one that "
             "explains today's problem. 0 = never purge anything.",
    )
    magento_log_error_count = fields.Integer(
        string="Unhandled sync problems", compute='_compute_magento_log_error_count',
    )
    # ── Stock cron ─────────────────────────────────────────────
    magento_stock_batch_size = fields.Integer(
        string="Products per batch",
        config_parameter='artaza_magento_connect.stock_batch_size',
        default=50,
        help="How many products the cron sends per push (for testing, use 5).",
    )
    magento_cron_active = fields.Boolean(string="Automatic stock sync")
    magento_cron_interval_number = fields.Integer(string="Frequency", default=30)
    magento_cron_interval_type = fields.Selection(
        [
            ('minutes', "Minutes"),
            ('hours', "Hours"),
            ('days', "Days"),
            ('weeks', "Weeks"),
        ],
        string="Unit",
        default='minutes',
    )

    # ── Order import (Magento → Odoo) ──────────────────────────
    # Payment methods that produce PAID orders (state processing/complete) → confirmed sale.
    magento_processing_methods = fields.Char(
        string="Immediate-payment methods (processing)",
        config_parameter='artaza_magento_connect.processing_methods',
        default='mercadopago_adbpayment_checkout_pro',
        help="Codes (comma-separated) of methods whose payment clears "
             "instantly. Their orders are imported as a confirmed sale, with no "
             "price change. E.g.: mercadopago_adbpayment_checkout_pro",
    )
    # Payment methods that produce UNPAID orders (state new) → draft quotation (negotiable).
    magento_pending_methods = fields.Char(
        string="Payment-pending methods (pending)",
        config_parameter='artaza_magento_connect.pending_methods',
        default='checkmo,banktransfer',
        help="Codes (comma-separated) of offline methods awaiting settlement. "
             "Their orders are imported as a quotation to negotiate: you can "
             "adjust the price and inform Magento. E.g.: checkmo, banktransfer",
    )
    # Tax applied to the shipping line of imported orders. The freight's IVA is a
    # fiscal decision (usually 21%, its own rate) — NOT the product's rate.
    magento_shipping_tax_id = fields.Many2one(
        'account.tax',
        string="Shipping tax",
        config_parameter='artaza_magento_connect.shipping_tax_id',
        domain="[('type_tax_use', '=', 'sale')]",
        help="IVA applied to the shipping line. Pick the PRICE-INCLUDED sale tax "
             "your accountant uses for freight (usually IVA 21%). Leave empty to "
             "mirror the products' tax (fine only if all your products share the "
             "same rate).",
    )
    # The pull cursor, on screen. It is a plain watermark: the import asks Magento
    # for everything updated at or after it. Moving it BACK re-reads that window,
    # which is safe — orders are create-once by their Magento number, so anything
    # already in Odoo is skipped and only what is missing comes in.
    magento_orders_cursor = fields.Datetime(
        string="Import orders updated since",
        help="Only orders updated at or after this moment are imported. It moves "
             "forward on its own as orders come in. Move it back to re-read a "
             "period (orders already in Odoo are skipped, never duplicated); "
             "move it forward to ignore history when connecting an existing store.",
    )
    magento_orders_cron_active = fields.Boolean(string="Import orders automatically")
    magento_orders_interval_number = fields.Integer(string="Orders frequency", default=15)
    magento_orders_interval_type = fields.Selection(
        [
            ('minutes', "Minutes"),
            ('hours', "Hours"),
            ('days', "Days"),
        ],
        string="Orders unit",
        default='minutes',
    )

    # ── Return (RMA) import (Magento → Odoo) ───────────────────
    magento_rmas_cursor = fields.Datetime(
        string="Import returns updated since",
        help="Same watermark as the orders one, for returns (RMA). Returns are "
             "create-once too, so moving it back re-reads without duplicating.",
    )
    magento_rmas_cron_active = fields.Boolean(string="Import RMAs automatically")
    magento_rmas_interval_number = fields.Integer(string="RMAs frequency", default=15)
    magento_rmas_interval_type = fields.Selection(
        [
            ('minutes', "Minutes"),
            ('hours', "Hours"),
            ('days', "Days"),
        ],
        string="RMAs unit",
        default='minutes',
    )

    def _compute_magento_log_error_count(self):
        count = self.env['artaza.magento.sync.log'].search_count([
            ('state', 'in', ('error', 'partial')),
            ('resolved', '=', False),
        ])
        for settings in self:
            settings.magento_log_error_count = count

    def action_magento_open_sync_log(self):
        self.ensure_one()
        return self.env['ir.actions.act_window']._for_xml_id(
            'artaza_magento_connect.action_magento_sync_log',
        )

    def _magento_stock_cron(self):
        return self.env.ref(CRON_XMLID, raise_if_not_found=False)

    def _magento_orders_cron(self):
        return self.env.ref(ORDERS_CRON_XMLID, raise_if_not_found=False)

    def _magento_rmas_cron(self):
        return self.env.ref(RMAS_CRON_XMLID, raise_if_not_found=False)

    def action_magento_pull_rmas(self):
        """Import returns (RMA) from Magento now and show how many came in."""
        self.ensure_one()
        count = self.env['artaza.magento.rma']._cron_magento_pull_rmas()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Returns imported"),
                'message': self.env._("%s new return(s) imported from Magento.", count),
                'sticky': False,
            },
        }

    def action_magento_pull_orders(self):
        """Import orders from Magento now and show how many new ones came in."""
        self.ensure_one()
        sale_order = self.env['sale.order']
        before = sale_order.search_count([('magento_order_id', '!=', False)])
        sale_order._cron_magento_pull_orders()
        count = sale_order.search_count([('magento_order_id', '!=', False)]) - before
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Orders imported"),
                'message': self.env._("%s new order(s) imported from Magento.", count),
                'sticky': False,
            },
        }

    # ── v4: direct Magento connection + mapping screens ────────
    def _magento_notify(self, kind, title, message, sticky=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': kind,
                'title': title,
                'message': message,
                'sticky': sticky,
            },
        }

    def _magento_persist_connection(self):
        """Save the connection fields before a button uses them.

        Without this, typing the URL/token and hitting a button straight away
        fails with "not configured", because settings are only written on Save.
        """
        self.ensure_one()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param(PARAM_MAGENTO_URL, (self.magento_base_url or '').strip())
        icp.set_param(PARAM_MAGENTO_TOKEN, (self.magento_token or '').strip())
        # Mirror res.config.settings' own semantics: an unticked Boolean
        # removes the parameter instead of storing the string 'False'
        # (`bool('False')` is True — that is the trap this avoids).
        icp.set_param(
            PARAM_MAGENTO_SKIP_SSL, 'True' if self.magento_skip_ssl_verify else False,
        )

    def action_magento_test_direct_connection(self):
        """Validate the Magento URL + integration token."""
        self.ensure_one()
        self._magento_persist_connection()
        stores = self.env['artaza.magento.client'].test_connection()
        codes = ", ".join(store.get('code') or '' for store in (stores or []))
        return self._magento_notify(
            'success',
            self.env._("Magento connection OK"),
            self.env._("Store views found: %s", codes or '-'),
        )

    def action_magento_refresh_sources(self):
        """Bring the MSI inventory sources over so they can be picked by name."""
        self.ensure_one()
        self._magento_persist_connection()
        log = self.env['artaza.magento.sync.log']
        with log.track('config', 'in', 'ui', note="Inventory sources") as tracker:
            created, updated = self.env['artaza.magento.source'].refresh_from_magento()
            tracker.ok(self.env['artaza.magento.source'].search([]).mapped('code'))
        return self._magento_notify(
            'success',
            self.env._("Sources updated"),
            self.env._("%(new)s new, %(known)s already known.",
                       new=created, known=updated),
        )

    def action_magento_refresh_tax_classes(self):
        """Bring the product tax classes over, with the rate their rules apply."""
        self.ensure_one()
        self._magento_persist_connection()
        log = self.env['artaza.magento.sync.log']
        with log.track('config', 'in', 'ui', note="Product tax classes") as tracker:
            created, updated = self.env['artaza.magento.tax.class'].refresh_from_magento()
            tracker.ok(self.env['artaza.magento.tax.class'].search([]).mapped('name'))
        return self._magento_notify(
            'success',
            self.env._("Tax classes updated"),
            self.env._("%(new)s new, %(known)s already known.",
                       new=created, known=updated),
        )

    def action_magento_automatch_taxes(self):
        """Match every unmapped sale tax to its Magento class by rate."""
        self.ensure_one()
        self._magento_persist_connection()
        taxes = self.env['account.tax'].search([
            ('type_tax_use', 'in', ('sale', 'all')),
        ])
        matched = taxes.action_magento_auto_match()
        # A mapped tax that is not price-included silently inflates every
        # imported order — worth shouting about, not a footnote.
        gross_issues = taxes._magento_price_include_warnings()
        if gross_issues:
            return self._magento_notify(
                'danger',
                self.env._("Check 'Tax-included price'"),
                self.env._(
                    "%(matched)s matched, but these taxes are NOT price-included: "
                    "%(taxes)s. Magento sends gross prices, so imported orders "
                    "would add the tax on top and their total would not match "
                    "Magento's. Tick 'Included in Price' on them in Accounting.",
                    matched=matched, taxes=", ".join(gross_issues.mapped('name')),
                ),
                sticky=True,
            )
        pending = taxes.filtered(lambda t: not t.magento_tax_class_id)
        if pending:
            return self._magento_notify(
                'warning',
                self.env._("Auto-match finished"),
                self.env._(
                    "%(matched)s matched. Still to choose by hand: %(pending)s",
                    matched=matched,
                    pending=", ".join(pending.mapped('name')),
                ),
                sticky=True,
            )
        return self._magento_notify(
            'success',
            self.env._("Auto-match finished"),
            self.env._("%s tax(es) matched. Nothing pending.", matched),
        )

    def action_magento_resync_all_taxes(self):
        """Mark ALL syncable products as pending so their tax class is re-pushed."""
        self.ensure_one()
        products = self.env['product.product'].search([
            ('is_storable', '=', True), ('default_code', '!=', False),
        ])
        products.write({'magento_tax_dirty': True})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Tax re-sync"),
                'message': self.env._(
                    "%s product(s) marked. The cron will send them in batches.",
                    len(products),
                ),
                'sticky': False,
            },
        }

    # ── Cron frequency (read/written on the ir.cron) ───────────
    @api.model
    def get_values(self):
        res = super().get_values()
        # The cursors are read and written by hand rather than through
        # `config_parameter`: they are stored as the plain UTC string Magento
        # filters on, and going through the generic Datetime conversion would
        # risk reformatting the one value the whole import depends on.
        icp = self.env['ir.config_parameter'].sudo()
        res.update(
            magento_orders_cursor=fields.Datetime.to_datetime(
                icp.get_param(ORDERS_CURSOR_PARAM) or ORDERS_DEFAULT_CURSOR,
            ),
            magento_rmas_cursor=fields.Datetime.to_datetime(
                icp.get_param(RMAS_CURSOR_PARAM) or RMAS_DEFAULT_CURSOR,
            ),
        )
        # Mapping tabs list the real records, so the operator maps by name.
        res.update(
            magento_warehouse_ids=[
                (6, 0, self.env['stock.warehouse'].search([]).ids),
            ],
            magento_sale_tax_ids=[
                (6, 0, self.env['account.tax'].search([
                    ('type_tax_use', 'in', ('sale', 'all')),
                ]).ids),
            ],
        )
        stock_cron = self.env.ref(CRON_XMLID, raise_if_not_found=False)
        if stock_cron:
            res.update(
                magento_cron_active=stock_cron.active,
                magento_cron_interval_number=stock_cron.interval_number,
                magento_cron_interval_type=stock_cron.interval_type,
            )
        orders_cron = self.env.ref(ORDERS_CRON_XMLID, raise_if_not_found=False)
        if orders_cron:
            res.update(
                magento_orders_cron_active=orders_cron.active,
                magento_orders_interval_number=orders_cron.interval_number,
                magento_orders_interval_type=orders_cron.interval_type,
            )
        rmas_cron = self.env.ref(RMAS_CRON_XMLID, raise_if_not_found=False)
        if rmas_cron:
            res.update(
                magento_rmas_cron_active=rmas_cron.active,
                magento_rmas_interval_number=rmas_cron.interval_number,
                magento_rmas_interval_type=rmas_cron.interval_type,
            )
        return res

    def set_values(self):
        super().set_values()
        icp = self.env['ir.config_parameter'].sudo()
        # Emptying the field means "from the beginning", not "from now": a blank
        # cursor that silently became today would skip the store's whole history.
        icp.set_param(
            ORDERS_CURSOR_PARAM,
            fields.Datetime.to_string(self.magento_orders_cursor)
            if self.magento_orders_cursor else ORDERS_DEFAULT_CURSOR,
        )
        icp.set_param(
            RMAS_CURSOR_PARAM,
            fields.Datetime.to_string(self.magento_rmas_cursor)
            if self.magento_rmas_cursor else RMAS_DEFAULT_CURSOR,
        )
        stock_cron = self._magento_stock_cron()
        if stock_cron:
            stock_cron.write({
                'active': self.magento_cron_active,
                'interval_number': max(1, self.magento_cron_interval_number or 1),
                'interval_type': self.magento_cron_interval_type,
            })
        orders_cron = self._magento_orders_cron()
        if orders_cron:
            orders_cron.write({
                'active': self.magento_orders_cron_active,
                'interval_number': max(1, self.magento_orders_interval_number or 1),
                'interval_type': self.magento_orders_interval_type,
            })
        rmas_cron = self._magento_rmas_cron()
        if rmas_cron:
            rmas_cron.write({
                'active': self.magento_rmas_cron_active,
                'interval_number': max(1, self.magento_rmas_interval_number or 1),
                'interval_type': self.magento_rmas_interval_type,
            })

    def action_magento_resync_all_stock(self):
        """Mark ALL syncable products as pending (full re-sync)."""
        self.ensure_one()
        count = self.env['product.product'].magento_mark_all_dirty()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Stock re-sync"),
                'message': self.env._(
                    "%s product(s) marked. The cron will send them in batches.", count,
                ),
                'sticky': False,
            },
        }
