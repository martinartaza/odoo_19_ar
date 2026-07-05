import logging

import requests

from odoo import api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Claves usadas en ir.config_parameter (ver integration_v2.md §6.1, §12)
PARAM_BASE_URL = 'artaza_magento_connect.middleware_base_url'
PARAM_API_KEY = 'artaza_magento_connect.api_key'

REQUEST_TIMEOUT = 20


class MagentoConnector(models.AbstractModel):
    """Cliente HTTP hacia el middleware FastAPI.

    Autentica con una API key (enviada como ``Authorization: Bearer <key>``).
    Es un AbstractModel: se usa vía ``self.env['artaza.magento.connector']``.
    """
    _name = 'artaza.magento.connector'
    _description = 'Cliente del middleware Magento (FastAPI)'

    # -- Configuración -------------------------------------------------------

    @api.model
    def _get_config(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base_url = (ICP.get_param(PARAM_BASE_URL) or '').rstrip('/')
        api_key = ICP.get_param(PARAM_API_KEY)
        if not (base_url and api_key):
            raise UserError(self.env._(
                "La integración con Magento no está configurada. "
                "Definí la URL del middleware y la API key en "
                "Ajustes ▸ Magento Connect."
            ))
        return {'base_url': base_url, 'api_key': api_key}

    # -- Llamada genérica ----------------------------------------------------

    @api.model
    def call(self, method, endpoint, payload=None):
        """Ejecuta una llamada autenticada al middleware.

        :param method: 'POST' | 'PUT' | 'GET' | 'DELETE'
        :param endpoint: ruta relativa, p.ej. 'cms/pages' o 'cms/pages/<id>'
        :param payload: dict a enviar como JSON (para POST/PUT)
        :return: el cuerpo JSON de la respuesta (dict)
        """
        cfg = self._get_config()
        url = '%s/%s' % (cfg['base_url'], endpoint.lstrip('/'))
        headers = {'Authorization': 'Bearer %s' % cfg['api_key']}

        try:
            resp = requests.request(
                method, url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            detail = self._extract_error(exc.response)
            raise UserError(self.env._(
                "El middleware rechazó la operación (%(code)s): %(detail)s",
                code=exc.response.status_code if exc.response is not None else '?',
                detail=detail,
            )) from exc
        except requests.RequestException as exc:
            raise UserError(self.env._(
                "Error de conexión con el middleware: %s", exc
            )) from exc

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    @api.model
    def test_connection(self):
        """Valida la API key + conexión contra el endpoint /ping del middleware."""
        return self.call('GET', 'ping')

    @api.model
    def sync_warehouses(self):
        """Registra las bodegas de Odoo en el middleware.

        Manda `[{code, name, is_default}]` (la principal = la primera por id)
        y devuelve el status `{received, mapped, pending, pending_warehouses}`.
        """
        Warehouse = self.env['stock.warehouse']
        warehouses = Warehouse.search([])
        default_wh = Warehouse.search([], order='id', limit=1)
        payload = [{
            'code': wh.code,
            'name': wh.name,
            'is_default': wh.id == default_wh.id,
        } for wh in warehouses]
        return self.call('POST', 'warehouses/sync', payload)

    @staticmethod
    def _extract_error(response):
        if response is None:
            return 'sin respuesta'
        try:
            body = response.json()
        except ValueError:
            return (response.text or '')[:500]
        err = body.get('error') or {}
        return err.get('message') or body.get('message') or str(body)[:500]
