from odoo import api, fields, models

from .magento_connector import (
    PARAM_API_KEY,
    PARAM_BASE_URL,
)

CRON_XMLID = 'artaza_magento_connect.ir_cron_magento_stock_sync'
ORDERS_CRON_XMLID = 'artaza_magento_connect.ir_cron_magento_pull_orders'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    magento_middleware_base_url = fields.Char(
        string="URL del middleware",
        config_parameter=PARAM_BASE_URL,
        help="Base URL de la API del middleware FastAPI, "
             "p.ej. https://www.artaza.net/api/v1",
    )
    magento_api_key = fields.Char(
        string="API key",
        config_parameter=PARAM_API_KEY,
        help="API key generada en el panel del middleware. "
             "Se envía como 'Authorization: Bearer <key>'.",
    )

    # ── Cron de stock ──────────────────────────────────────────
    magento_stock_batch_size = fields.Integer(
        string="Productos por lote",
        config_parameter='artaza_magento_connect.stock_batch_size',
        default=50,
        help="Cuántos productos manda el cron por envío (para probar, poné 5).",
    )
    magento_cron_active = fields.Boolean(string="Sync automática de stock")
    magento_cron_interval_number = fields.Integer(string="Frecuencia", default=30)
    magento_cron_interval_type = fields.Selection(
        [
            ('minutes', "Minutos"),
            ('hours', "Horas"),
            ('days', "Días"),
            ('weeks', "Semanas"),
        ],
        string="Unidad",
        default='minutes',
    )

    # ── Importación de órdenes (Magento → Odoo) ────────────────
    magento_offline_methods = fields.Char(
        string="Métodos de pago offline",
        config_parameter='artaza_magento_connect.offline_methods',
        default='banktransfer',
        help="Códigos de métodos offline (separados por coma) cuyas órdenes "
             "pendientes se absorben como presupuesto para negociar. Ej: banktransfer.",
    )
    magento_orders_cron_active = fields.Boolean(string="Importar órdenes automáticamente")
    magento_orders_interval_number = fields.Integer(string="Frecuencia órdenes", default=15)
    magento_orders_interval_type = fields.Selection(
        [
            ('minutes', "Minutos"),
            ('hours', "Horas"),
            ('days', "Días"),
        ],
        string="Unidad órdenes",
        default='minutes',
    )

    def _magento_stock_cron(self):
        return self.env.ref(CRON_XMLID, raise_if_not_found=False)

    def _magento_orders_cron(self):
        return self.env.ref(ORDERS_CRON_XMLID, raise_if_not_found=False)

    def action_magento_test_connection(self):
        """Valida la API key + conexión contra /ping del middleware."""
        self.ensure_one()
        self.env['artaza.magento.connector'].test_connection()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Conexión correcta"),
                'message': self.env._("El middleware respondió correctamente."),
                'sticky': False,
            },
        }

    def action_magento_sync_warehouses(self):
        """Registra las bodegas de Odoo en el middleware y muestra el estado."""
        self.ensure_one()
        result = self.env['artaza.magento.connector'].sync_warehouses()
        pending = result.get('pending_warehouses') or []
        if pending:
            kind = 'warning'
            message = self.env._(
                "Bodegas registradas. Faltan relacionar en el middleware: %s",
                ", ".join(pending),
            )
        else:
            kind = 'success'
            message = self.env._("Tus bodegas ya están sincronizadas con Magento.")
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': kind,
                'title': self.env._("Sincronización de bodegas"),
                'message': message,
                'sticky': bool(pending),
            },
        }

    # ── Frecuencia del cron (leída/escrita en el ir.cron) ──────
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

    def action_magento_resync_all_stock(self):
        """Marca TODOS los productos sincronizables como pendientes (full re-sync)."""
        self.ensure_one()
        count = self.env['product.product'].magento_mark_all_dirty()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Re-sincronización de stock"),
                'message': self.env._(
                    "%s producto(s) marcados. El cron los enviará en lotes.", count
                ),
                'sticky': False,
            },
        }
