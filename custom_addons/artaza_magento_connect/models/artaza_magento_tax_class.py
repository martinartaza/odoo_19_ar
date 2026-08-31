"""Mirror of a Magento **product tax class**, plus the auto-match by rate.

A Magento tax class has **no rate of its own**: the link is
``tax rule → (product tax classes, tax rates)``. So to say "this class is the
21%" the rules and the rates have to be read and crossed. That crossing is what
lets Odoo auto-match `IVA 21%` to the right class instead of asking a human for
an id — see ``integration_v4.md`` §3.4.

The matching logic is kept as **pure functions** so it stays trivially testable
outside Odoo.
"""
from odoo import api, fields, models

# Rates are percentages; tolerate float noise (21.0 vs 21.000000001).
RATE_TOLERANCE = 0.01


def rates_by_tax_class(rules, rates):
    """{product_tax_class_id: {rate percentages applied to it}}."""
    rate_by_id = {}
    for rate in rates:
        if rate.get('id') is None or rate.get('rate') is None:
            continue
        rate_by_id[int(rate['id'])] = float(rate['rate'])

    result = {}
    for rule in rules:
        rule_rates = {
            rate_by_id[int(rid)]
            for rid in (rule.get('tax_rate_ids') or [])
            if int(rid) in rate_by_id
        }
        if not rule_rates:
            continue
        for class_id in rule.get('product_tax_class_ids') or []:
            result.setdefault(int(class_id), set()).update(rule_rates)
    return result


def match_class_for_rate(amount, class_rates):
    """The Magento tax class that applies exactly `amount`, if unambiguous.

    Returns None when the rate is unknown, when no class applies it, when
    several do, or when the class also applies other rates — every case a human
    must resolve rather than the system guessing. Same rule as a warehouse with
    no source: never write the wrong one.
    """
    if amount is None:
        return None
    candidates = [
        class_id
        for class_id, rates in class_rates.items()
        if len(rates) == 1 and abs(next(iter(rates)) - amount) <= RATE_TOLERANCE
    ]
    return candidates[0] if len(candidates) == 1 else None


class MagentoTaxClass(models.Model):
    _name = 'artaza.magento.tax.class'
    _description = 'Magento product tax class'
    _order = 'name'

    magento_class_id = fields.Integer(
        string="Magento class id", required=True, index=True,
        help="The `class_id` in Magento. Sent in the tax-class push payload.",
    )
    name = fields.Char(string="Name", required=True)
    rate = fields.Float(
        string="Rate (%)", digits=(5, 2),
        help="Rate the Magento tax rules apply to this class. Only meaningful "
             "when the class applies exactly one rate.",
    )
    single_rate = fields.Boolean(
        string="Single rate", default=False,
        help="False when the class applies several rates (or none): it cannot "
             "be auto-matched and must be chosen by hand.",
    )

    _magento_class_uniq = models.Constraint(
        'unique(magento_class_id)',
        'That Magento tax class is already registered.',
    )

    @api.depends('name', 'rate', 'single_rate')
    def _compute_display_name(self):
        for tax_class in self:
            if tax_class.single_rate:
                rate = ('%.2f' % tax_class.rate).rstrip('0').rstrip('.')
                tax_class.display_name = '%s — %s%%' % (tax_class.name or '', rate)
            else:
                tax_class.display_name = self.env._(
                    "%s — no single rate", tax_class.name or '',
                )

    @api.model
    def refresh_from_magento(self):
        """Re-read the classes and the rates their rules apply. (created, updated)."""
        client = self.env['artaza.magento.client']
        classes = client.fetch_product_tax_classes()
        class_rates = rates_by_tax_class(
            client.fetch_tax_rules(), client.fetch_tax_rates(),
        )

        existing = {tc.magento_class_id: tc for tc in self.search([])}
        created = updated = 0
        for item in classes:
            class_id = item.get('class_id')
            if class_id is None:
                continue
            class_id = int(class_id)
            rates = class_rates.get(class_id) or set()
            values = {
                'name': item.get('class_name') or str(class_id),
                'rate': next(iter(rates)) if len(rates) == 1 else 0.0,
                'single_rate': len(rates) == 1,
            }
            tax_class = existing.get(class_id)
            if tax_class:
                tax_class.write(values)
                updated += 1
            else:
                self.create(dict(values, magento_class_id=class_id))
                created += 1
        return created, updated
