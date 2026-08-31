"""Mirror of a Magento MSI inventory source, so warehouses map by name.

Why a mirror model and not a Char with the source code: a `Many2one` gives the
operator a dropdown of what actually exists in Magento. Typing a code (or worse,
an id copied from a URL) is what made the tax-class rollout fail silently on
2026-08-06 — see ``integration_v4.md`` §3.4.

These records are a **cache**: the button in the settings screen refreshes them
from Magento. Nothing is ever created here by hand.
"""
from odoo import api, fields, models


class MagentoSource(models.Model):
    _name = 'artaza.magento.source'
    _description = 'Magento inventory source'
    _order = 'name'

    code = fields.Char(
        string="Source code", required=True, index=True,
        help="The `source_code` in Magento. Used in the stock push payload.",
    )
    name = fields.Char(string="Name", required=True)
    enabled = fields.Boolean(
        string="Enabled in Magento", default=True,
        help="Mirrors the source's own enabled flag in Magento.",
    )

    _source_code_uniq = models.Constraint(
        'unique(code)',
        'That Magento source is already registered.',
    )

    @api.depends('name', 'code')
    def _compute_display_name(self):
        for source in self:
            source.display_name = '%s (%s)' % (source.name or '', source.code or '')

    @api.model
    def refresh_from_magento(self):
        """Re-read the sources from Magento. Returns (created, updated).

        Upsert by `code`: a source that disappeared in Magento is left alone
        rather than deleted, because a warehouse may still point at it and
        losing that mapping silently is exactly what v4 is trying to avoid.
        """
        items = self.env['artaza.magento.client'].fetch_sources()
        existing = {source.code: source for source in self.search([])}
        created = updated = 0
        for item in items:
            code = item.get('source_code')
            if not code:
                continue
            values = {
                'name': item.get('name') or code,
                'enabled': bool(item.get('enabled', True)),
            }
            source = existing.get(code)
            if source:
                source.write(values)
                updated += 1
            else:
                self.create(dict(values, code=code))
                created += 1
        return created, updated
