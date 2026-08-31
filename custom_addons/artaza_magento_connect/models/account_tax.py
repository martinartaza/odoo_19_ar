"""Sale tax → Magento product tax class mapping, with auto-match by rate.

The tax rate is the only thing both systems agree on: Odoo knows `IVA 21%` pays
21%, Magento knows which class its rules charge 21% to. Matching on that is what
removes the id-typing entirely for the common case.

Ambiguity is never guessed — it is left pending and highlighted, exactly like a
warehouse with no source. See ``integration_v4.md`` §3.4.
"""
from odoo import api, fields, models

from .artaza_magento_tax_class import match_class_for_rate, rates_by_tax_class


class AccountTax(models.Model):
    _inherit = 'account.tax'

    magento_tax_class_id = fields.Many2one(
        'artaza.magento.tax.class',
        string="Magento tax class",
        ondelete='restrict',
        help="Product tax class assigned in Magento to products carrying this "
             "tax. Empty = pending: those products' tax class is NOT pushed.",
    )

    magento_price_include_ok = fields.Boolean(
        string="Tax-included price", compute='_compute_magento_price_include_ok',
        help="False = this tax is NOT price-included, but the integration assumes "
             "it is (Magento runs 'Catalog Prices = Including Tax'). Imported "
             "orders would add the tax ON TOP and their total would not match "
             "Magento's.",
    )

    @api.depends('price_include')
    def _compute_magento_price_include_ok(self):
        for tax in self:
            tax.magento_price_include_ok = bool(tax.price_include)

    def _magento_rate(self):
        """The rate to match on, or None when the tax has no single rate.

        Only 'percent' and 'division' express a rate; a fixed-amount tax or a
        tax group has nothing to compare against and stays pending.
        """
        self.ensure_one()
        if self.amount_type in ('percent', 'division'):
            return self.amount
        return None

    def action_magento_auto_match(self):
        """Auto-match these taxes to a Magento class by rate.

        Never overwrites a class a human already set, and never guesses an
        ambiguous one. Returns the number matched.
        """
        client = self.env['artaza.magento.client']
        class_rates = rates_by_tax_class(
            client.fetch_tax_rules(), client.fetch_tax_rates(),
        )
        classes_by_magento_id = {
            tax_class.magento_class_id: tax_class
            for tax_class in self.env['artaza.magento.tax.class'].search([])
        }
        # Only match against classes that exist as product classes in Odoo's mirror.
        class_rates = {
            class_id: rates
            for class_id, rates in class_rates.items()
            if class_id in classes_by_magento_id
        }

        matched = 0
        for tax in self:
            if tax.magento_tax_class_id:
                continue
            found = match_class_for_rate(tax._magento_rate(), class_rates)
            if found is not None:
                tax.magento_tax_class_id = classes_by_magento_id[found]
                matched += 1
        return matched

    def _magento_price_include_warnings(self):
        """Mapped taxes that are NOT price-included — a silent money bug.

        Magento stores and shows prices tax-included, so an order line arrives
        with the gross unit price. If the Odoo tax is not price-included, Odoo
        adds the tax again on top: the sales order total stops matching
        Magento's grand total, and the negotiation would compute an adjustment
        out of thin air. Cheap to detect, expensive to notice by hand.
        """
        return self.filtered(
            lambda tax: tax.magento_tax_class_id and not tax.price_include,
        )
