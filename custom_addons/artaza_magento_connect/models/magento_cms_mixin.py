import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Contexto para evitar recursión cuando escribimos el magento_id de vuelta
SKIP_SYNC_CTX = 'skip_magento_sync'


class MagentoCmsMixin(models.AbstractModel):
    """Comportamiento común a las entidades CMS sincronizables con Magento.

    Maneja la clave natural ``identifier``, el estado de sincronización y el
    push inmediato al middleware en create/write (ver integration.md §3).
    Los modelos concretos definen su recurso y su payload específico.
    """
    _name = 'artaza.magento.cms.mixin'
    _description = 'Mixin CMS Magento'

    name = fields.Char(string="Título", required=True)
    identifier = fields.Char(
        string="Identifier (URL key)",
        required=True,
        copy=False,
        help="Clave natural única que vincula el contenido en Odoo y Magento. "
             "No debe cambiar tras la creación.",
    )
    active = fields.Boolean(default=True)
    content = fields.Html(string="Contenido", sanitize=False)
    store_ids = fields.Char(
        string="Store views",
        default="0",
        help="IDs de store-view de Magento separados por coma. 0 = vista por defecto/admin.",
    )

    magento_id = fields.Integer(string="ID en Magento", readonly=True, copy=False)
    sync_state = fields.Selection(
        [
            ('pending', "Pendiente"),
            ('synced', "Sincronizado"),
            ('error', "Error"),
        ],
        string="Estado de sync",
        default='pending',
        readonly=True,
        copy=False,
    )
    sync_error = fields.Text(string="Último error", readonly=True, copy=False)
    last_sync = fields.Datetime(string="Última sincronización", readonly=True, copy=False)

    _sql_constraints = [
        ('identifier_uniq', 'unique(identifier)',
         "El identifier debe ser único."),
    ]

    # -- A implementar por cada modelo concreto ------------------------------

    def _magento_resource(self):
        """Ruta base del recurso en el middleware, p.ej. 'cms/pages'."""
        raise NotImplementedError

    def _prepare_payload(self):
        """Payload base común. Los modelos concretos extienden vía super()."""
        self.ensure_one()
        return {
            'source': 'odoo',
            'source_id': self.id,
            'identifier': self.identifier,
            'title': self.name,
            'active': self.active,
            'content': self.content or '',
            'store_id': self._store_id_list(),
            'magento_id': self.magento_id or None,
        }

    def _store_id_list(self):
        self.ensure_one()
        ids = []
        for chunk in (self.store_ids or '0').split(','):
            chunk = chunk.strip()
            if chunk:
                try:
                    ids.append(int(chunk))
                except ValueError:
                    continue
        return ids or [0]

    # -- Validaciones --------------------------------------------------------

    @api.constrains('identifier')
    def _check_identifier(self):
        for rec in self:
            if rec.identifier and ' ' in rec.identifier:
                raise ValidationError(self.env._(
                    "El identifier no puede contener espacios: %s", rec.identifier
                ))

    # -- Sincronización ------------------------------------------------------

    def _sync_to_middleware(self):
        """Envía el contenido al middleware. POST si es nuevo, PUT si ya existe."""
        connector = self.env['artaza.magento.connector']
        for rec in self:
            payload = rec._prepare_payload()
            if rec.magento_id:
                method = 'PUT'
                endpoint = '%s/%s' % (rec._magento_resource(), rec.identifier)
            else:
                method = 'POST'
                endpoint = rec._magento_resource()
            try:
                result = connector.call(method, endpoint, payload)
            except Exception as exc:  # noqa: BLE001 - registramos y marcamos error
                _logger.warning("Sync Magento falló para %s (%s): %s",
                                rec._name, rec.identifier, exc)
                rec.with_context(**{SKIP_SYNC_CTX: True}).write({
                    'sync_state': 'error',
                    'sync_error': str(exc),
                })
                continue
            vals = {
                'sync_state': 'synced',
                'sync_error': False,
                'last_sync': fields.Datetime.now(),
            }
            mid = result.get('magento_id')
            if mid:
                vals['magento_id'] = mid
            rec.with_context(**{SKIP_SYNC_CTX: True}).write(vals)

    def action_sync_now(self):
        """Botón manual: reintenta la sincronización."""
        self._sync_to_middleware()
        return True

    # -- Push inmediato al guardar (integration.md §3) -----------------------

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get(SKIP_SYNC_CTX):
            records._sync_to_middleware()
        return records

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get(SKIP_SYNC_CTX):
            return res
        # Solo re-sincronizar si cambió contenido relevante (no los campos de estado).
        tracked = set(vals) - {
            'magento_id', 'sync_state', 'sync_error', 'last_sync',
        }
        if tracked:
            self._sync_to_middleware()
        return res
