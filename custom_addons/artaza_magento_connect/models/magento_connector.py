import logging
from datetime import timedelta

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Claves usadas en ir.config_parameter (ver integration.md §10)
PARAM_BASE_URL = 'artaza_magento_connect.middleware_base_url'
PARAM_CLIENT_ID = 'artaza_magento_connect.client_id'
PARAM_CLIENT_SECRET = 'artaza_magento_connect.client_secret'
# Cache del token JWT
PARAM_TOKEN = 'artaza_magento_connect.access_token'
PARAM_TOKEN_EXPIRY = 'artaza_magento_connect.token_expiry'

# Margen de seguridad antes de considerar el token expirado
TOKEN_SKEW = timedelta(seconds=60)
REQUEST_TIMEOUT = 20


class MagentoConnector(models.AbstractModel):
    """Cliente HTTP hacia el middleware FastAPI.

    Encapsula la autenticación OAuth2/JWT (client credentials) y las llamadas
    REST. Es un AbstractModel: se usa vía ``self.env['artaza.magento.connector']``.
    """
    _name = 'artaza.magento.connector'
    _description = 'Cliente del middleware Magento (FastAPI)'

    # -- Configuración -------------------------------------------------------

    @api.model
    def _get_config(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base_url = (ICP.get_param(PARAM_BASE_URL) or '').rstrip('/')
        client_id = ICP.get_param(PARAM_CLIENT_ID)
        client_secret = ICP.get_param(PARAM_CLIENT_SECRET)
        if not (base_url and client_id and client_secret):
            raise UserError(self.env._(
                "La integración con Magento no está configurada. "
                "Define la URL del middleware y las credenciales en "
                "Ajustes ▸ Magento Connect."
            ))
        return {
            'base_url': base_url,
            'client_id': client_id,
            'client_secret': client_secret,
        }

    # -- Autenticación JWT ---------------------------------------------------

    @api.model
    def _get_token(self, force=False):
        """Devuelve un access_token válido, reutilizando el cacheado si no expiró."""
        ICP = self.env['ir.config_parameter'].sudo()
        if not force:
            token = ICP.get_param(PARAM_TOKEN)
            expiry = ICP.get_param(PARAM_TOKEN_EXPIRY)
            if token and expiry:
                try:
                    expiry_dt = fields.Datetime.to_datetime(expiry)
                except ValueError:
                    expiry_dt = None
                if expiry_dt and fields.Datetime.now() + TOKEN_SKEW < expiry_dt:
                    return token

        cfg = self._get_config()
        url = '%s/auth/token' % cfg['base_url']
        try:
            resp = requests.post(
                url,
                json={
                    'grant_type': 'client_credentials',
                    'client_id': cfg['client_id'],
                    'client_secret': cfg['client_secret'],
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise UserError(self.env._(
                "No se pudo obtener el token del middleware: %s", exc
            )) from exc

        token = data.get('access_token')
        if not token:
            raise UserError(self.env._("El middleware no devolvió un access_token."))
        expires_in = int(data.get('expires_in') or 3600)
        expiry_dt = fields.Datetime.now() + timedelta(seconds=expires_in)
        ICP.set_param(PARAM_TOKEN, token)
        ICP.set_param(PARAM_TOKEN_EXPIRY, fields.Datetime.to_string(expiry_dt))
        return token

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

        def _do(token):
            return requests.request(
                method,
                url,
                json=payload,
                headers={'Authorization': 'Bearer %s' % token},
                timeout=REQUEST_TIMEOUT,
            )

        token = self._get_token()
        try:
            resp = _do(token)
            # JWT expirado/ inválido -> renovar una vez y reintentar (integration.md §9)
            if resp.status_code == 401:
                token = self._get_token(force=True)
                resp = _do(token)
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
