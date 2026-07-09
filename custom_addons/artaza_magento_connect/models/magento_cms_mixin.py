import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Context flag to avoid recursion when we write magento_id back
SKIP_SYNC_CTX = 'skip_magento_sync'


class MagentoCmsMixin(models.AbstractModel):
    """Common behaviour for CMS entities synced with Magento.

    Handles the ``identifier`` natural key, the sync state and the immediate
    push to the middleware on create/write (see integration.md §3). Concrete
    models define their resource and their specific payload.
    """
    _name = 'artaza.magento.cms.mixin'
    _description = 'Magento CMS Mixin'

    name = fields.Char(string="Title", required=True)
    identifier = fields.Char(
        string="Identifier (URL key)",
        required=True,
        copy=False,
        help="Unique natural key linking the content in Odoo and Magento. "
             "Must not change after creation.",
    )
    active = fields.Boolean(default=True)
    content = fields.Html(string="Content", sanitize=False)
    store_ids = fields.Char(
        string="Store views",
        default="0",
        help="Magento store-view IDs separated by commas. 0 = default/admin view.",
    )

    magento_id = fields.Integer(string="Magento ID", readonly=True, copy=False)
    sync_state = fields.Selection(
        [
            ('pending', "Pending"),
            ('synced', "Synced"),
            ('error', "Error"),
        ],
        string="Sync state",
        default='pending',
        readonly=True,
        copy=False,
    )
    sync_error = fields.Text(string="Last error", readonly=True, copy=False)
    last_sync = fields.Datetime(string="Last sync", readonly=True, copy=False)

    _sql_constraints = [
        ('identifier_uniq', 'unique(identifier)',
         "The identifier must be unique."),
    ]

    # -- To be implemented by each concrete model ----------------------------

    def _magento_resource(self):
        """Base resource path in the middleware, e.g. 'cms/pages'."""
        raise NotImplementedError

    def _prepare_payload(self):
        """Common base payload. Concrete models extend via super()."""
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

    # -- Validations ---------------------------------------------------------

    @api.constrains('identifier')
    def _check_identifier(self):
        for rec in self:
            if rec.identifier and ' ' in rec.identifier:
                raise ValidationError(self.env._(
                    "The identifier cannot contain spaces: %s", rec.identifier
                ))

    # -- Sync ----------------------------------------------------------------

    def _sync_to_middleware(self):
        """Send the content to the middleware. POST if new, PUT if it exists."""
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
            except Exception as exc:  # noqa: BLE001 - log and flag error
                _logger.warning("Magento sync failed for %s (%s): %s",
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
        """Manual button: retry the sync."""
        self._sync_to_middleware()
        return True

    # -- Immediate push on save (integration.md §3) --------------------------

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
        # Only re-sync if relevant content changed (not the state fields).
        tracked = set(vals) - {
            'magento_id', 'sync_state', 'sync_error', 'last_sync',
        }
        if tracked:
            self._sync_to_middleware()
        return res
