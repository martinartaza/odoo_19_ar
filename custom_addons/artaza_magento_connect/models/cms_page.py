from odoo import fields, models


class MagentoCmsPage(models.Model):
    """Magento CMS Page (cmsPageRepositoryV1). See integration.md §6.1."""
    _name = 'artaza.magento.cms.page'
    _inherit = 'artaza.magento.cms.mixin'
    _description = 'Magento CMS Page'

    content_heading = fields.Char(string="Content heading")
    page_layout = fields.Selection(
        [
            ('1column', "1 column"),
            ('2columns-left', "2 columns (left)"),
            ('2columns-right', "2 columns (right)"),
            ('3columns', "3 columns"),
            ('empty', "Empty"),
        ],
        string="Layout",
        default='1column',
    )
    meta_title = fields.Char(string="Meta title")
    meta_keywords = fields.Char(string="Meta keywords")
    meta_description = fields.Text(string="Meta description")
    sort_order = fields.Integer(string="Sort order", default=0)

    def _magento_resource(self):
        return 'cms/pages'

    def _prepare_payload(self):
        payload = super()._prepare_payload()
        self.ensure_one()
        payload.update({
            'content_heading': self.content_heading or '',
            'page_layout': self.page_layout or '1column',
            'meta_title': self.meta_title or '',
            'meta_keywords': self.meta_keywords or '',
            'meta_description': self.meta_description or '',
            'sort_order': str(self.sort_order or 0),
        })
        return payload
