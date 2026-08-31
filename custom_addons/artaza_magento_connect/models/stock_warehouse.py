"""Warehouse → Magento inventory source mapping.

Lives as a field on the warehouse itself rather than in a separate mapping
table: the relation is 1:1 and the warehouse already exists, so a column on it
is simpler and cannot get orphaned.
"""
from odoo import fields, models


class StockWarehouse(models.Model):
    _inherit = 'stock.warehouse'

    magento_source_id = fields.Many2one(
        'artaza.magento.source',
        string="Magento source",
        ondelete='restrict',
        help="Magento MSI source this warehouse's stock is pushed to. Empty = "
             "pending: the stock of this warehouse is NOT sent (never guessed).",
    )
