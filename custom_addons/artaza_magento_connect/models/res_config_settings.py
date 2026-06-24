from odoo import fields, models

from .magento_connector import (
    PARAM_BASE_URL,
    PARAM_CLIENT_ID,
    PARAM_CLIENT_SECRET,
)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    magento_middleware_base_url = fields.Char(
        string="URL del middleware",
        config_parameter=PARAM_BASE_URL,
        help="Base URL de la API del middleware FastAPI, "
             "p.ej. https://www.sebastianartaza.com/api/v1",
    )
    magento_client_id = fields.Char(
        string="Client ID",
        config_parameter=PARAM_CLIENT_ID,
    )
    magento_client_secret = fields.Char(
        string="Client Secret",
        config_parameter=PARAM_CLIENT_SECRET,
    )

    def action_magento_test_connection(self):
        """Fuerza la obtención de un token JWT para validar las credenciales."""
        self.ensure_one()
        self.env['artaza.magento.connector']._get_token(force=True)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': self.env._("Conexión correcta"),
                'message': self.env._("Se obtuvo un token JWT del middleware."),
                'sticky': False,
            },
        }
