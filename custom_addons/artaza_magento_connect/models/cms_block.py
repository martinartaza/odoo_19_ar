from odoo import models


class MagentoCmsBlock(models.Model):
    """CMS Block de Magento (cmsBlockRepositoryV1). Ver integration.md §6.2."""
    _name = 'artaza.magento.cms.block'
    _inherit = 'artaza.magento.cms.mixin'
    _description = 'Bloque CMS de Magento'

    def _magento_resource(self):
        return 'cms/blocks'
