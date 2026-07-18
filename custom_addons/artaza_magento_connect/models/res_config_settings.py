from odoo import api, fields, models

from .magento_connector import (
    PARAM_API_KEY,
    PARAM_BASE_URL,
)

CRON_XMLID = 'artaza_magento_connect.ir_cron_magento_stock_sync'
ORDERS_CRON_XMLID = 'artaza_magento_connect.ir_cron_magento_pull_orders'
RMAS_CRON_XMLID = 'artaza_magento_connect.ir_cron_magento_pull_rmas'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    magento_middleware_base_url = fields.Char(
        string="Middleware URL",
        config_parameter=PARAM_BASE_URL,
        help="Base URL of the FastAPI middleware API, "
             "e.g. https://www.artaza.net/api/v1",
    )
    magento_api_key = fields.Char(
        string="API key",
        config_parameter=PARAM_API_KEY,
        help="API key generated in the middleware panel. "
             "Sent as 'Authorization: Bearer <key>'.",
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

    def _magento_stock_cron(self):
        return self.env.ref(CRON_XMLID, raise_if_not_found=False)

    def _magento_orders_cron(self):
        return self.env.ref(ORDERS_CRON_XMLID, raise_if_not_found=False)

    def _magento_rmas_cron(self):
        return self.env.ref(RMAS_CRON_XMLID, raise_if_not_found=False)

    def action_magento_pull_rmas(self):
        """Import returns (RMA) from Magento now and show how many came in."""
        self.ensure_one()
        count = self.env['magento.rma']._cron_magento_pull_rmas()
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

    def action_magento_test_connection(self):
        """Validate the API key + connection against the middleware /ping."""
        self.ensure_one()
        self.env['artaza.magento.connector'].test_connection()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Connection OK"),
                'message': self.env._("The middleware responded correctly."),
                'sticky': False,
            },
        }

    def action_magento_sync_warehouses(self):
        """Register the Odoo warehouses in the middleware and show the status."""
        self.ensure_one()
        result = self.env['artaza.magento.connector'].sync_warehouses()
        pending = result.get('pending_warehouses') or []
        if pending:
            kind = 'warning'
            message = self.env._(
                "Warehouses registered. Still to be mapped in the middleware: %s",
                ", ".join(pending),
            )
        else:
            kind = 'success'
            message = self.env._("Your warehouses are already synced with Magento.")
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': kind,
                'title': self.env._("Warehouse sync"),
                'message': message,
                'sticky': bool(pending),
            },
        }

    # ── Cron frequency (read/written on the ir.cron) ───────────
    @api.model
    def get_values(self):
        res = super().get_values()
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
                    "%s product(s) marked. The cron will send them in batches.", count
                ),
                'sticky': False,
            },
        }
