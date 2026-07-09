import logging

import requests

from odoo import api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Keys used in ir.config_parameter (see integration_v2.md §6.1, §12)
PARAM_BASE_URL = 'artaza_magento_connect.middleware_base_url'
PARAM_API_KEY = 'artaza_magento_connect.api_key'

REQUEST_TIMEOUT = 20


class MagentoConnector(models.AbstractModel):
    """HTTP client to the FastAPI middleware.

    Authenticates with an API key (sent as ``Authorization: Bearer <key>``).
    It is an AbstractModel: used via ``self.env['artaza.magento.connector']``.
    """
    _name = 'artaza.magento.connector'
    _description = 'Magento middleware client (FastAPI)'

    # -- Configuration -------------------------------------------------------

    @api.model
    def _get_config(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base_url = (ICP.get_param(PARAM_BASE_URL) or '').rstrip('/')
        api_key = ICP.get_param(PARAM_API_KEY)
        if not (base_url and api_key):
            raise UserError(self.env._(
                "The Magento integration is not configured. Set the middleware "
                "URL and API key in Settings ▸ Magento Connect."
            ))
        return {'base_url': base_url, 'api_key': api_key}

    # -- Generic call --------------------------------------------------------

    @api.model
    def call(self, method, endpoint, payload=None):
        """Run an authenticated call to the middleware.

        :param method: 'POST' | 'PUT' | 'GET' | 'DELETE'
        :param endpoint: relative path, e.g. 'cms/pages' or 'cms/pages/<id>'
        :param payload: dict to send as JSON (for POST/PUT)
        :return: the JSON body of the response (dict)
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
                "The middleware rejected the operation (%(code)s): %(detail)s",
                code=exc.response.status_code if exc.response is not None else '?',
                detail=detail,
            )) from exc
        except requests.RequestException as exc:
            raise UserError(self.env._(
                "Connection error with the middleware: %s", exc
            )) from exc

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    @api.model
    def test_connection(self):
        """Validate the API key + connection against the middleware /ping endpoint."""
        return self.call('GET', 'ping')

    @api.model
    def sync_warehouses(self):
        """Register the Odoo warehouses in the middleware.

        Sends `[{code, name, is_default}]` (default = the first one by id) and
        returns the status `{received, mapped, pending, pending_warehouses}`.
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
            return 'no response'
        try:
            body = response.json()
        except ValueError:
            return (response.text or '')[:500]
        err = body.get('error') or {}
        return err.get('message') or body.get('message') or str(body)[:500]
