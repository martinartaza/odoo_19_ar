from odoo import fields, models

from .magento_connector import (
    PARAM_API_KEY,
    PARAM_BASE_URL,
)


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
