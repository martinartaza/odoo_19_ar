from odoo import models


class MagentoCmsBlock(models.Model):
    """Magento CMS Block (cmsBlockRepositoryV1). See integration.md §6.2."""
    _name = 'artaza.magento.cms.block'
    _inherit = 'artaza.magento.cms.mixin'
    _description = 'Magento CMS Block'

    def _magento_resource(self):
        return 'cms/blocks'
