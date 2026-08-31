"""Auto-match by rate — the logic that removes id-typing from the setup.

The rule under test is *never guess*: an ambiguous rate must stay pending, so a
human resolves it, rather than the connector writing the wrong tax class to
every product. Getting this wrong is silent and expensive.
"""
from odoo.tests.common import TransactionCase, tagged

from ..models.artaza_magento_tax_class import match_class_for_rate, rates_by_tax_class


@tagged('post_install', '-at_install')
class TestTaxMatch(TransactionCase):

    def test_rates_by_tax_class(self):
        rules = [{'tax_rate_ids': [1], 'product_tax_class_ids': [4]},
                 {'tax_rate_ids': [2], 'product_tax_class_ids': [7]}]
        rates = [{'id': 1, 'rate': 21.0}, {'id': 2, 'rate': 10.5}]
        self.assertEqual(rates_by_tax_class(rules, rates), {4: {21.0}, 7: {10.5}})

    def test_rates_by_tax_class_ignores_incomplete_rows(self):
        rules = [{'tax_rate_ids': [1, 99], 'product_tax_class_ids': [4]},
                 {'tax_rate_ids': [], 'product_tax_class_ids': [5]},
                 {'product_tax_class_ids': [6]}]
        rates = [{'id': 1, 'rate': 21.0}, {'id': None, 'rate': 5.0}, {'id': 3}]
        result = rates_by_tax_class(rules, rates)
        self.assertEqual(result, {4: {21.0}})   # 5 and 6 have no usable rate

    def test_one_class_can_gather_rates_from_several_rules(self):
        rules = [{'tax_rate_ids': [1], 'product_tax_class_ids': [4]},
                 {'tax_rate_ids': [2], 'product_tax_class_ids': [4]}]
        rates = [{'id': 1, 'rate': 21.0}, {'id': 2, 'rate': 10.5}]
        self.assertEqual(rates_by_tax_class(rules, rates), {4: {21.0, 10.5}})

    def test_match_unambiguous(self):
        self.assertEqual(match_class_for_rate(21.0, {4: {21.0}, 7: {10.5}}), 4)

    def test_match_tolerates_float_noise(self):
        self.assertEqual(match_class_for_rate(21.0, {4: {21.000000001}}), 4)

    def test_no_match_when_two_classes_share_the_rate(self):
        self.assertIsNone(match_class_for_rate(21.0, {4: {21.0}, 5: {21.0}}))

    def test_no_match_when_the_class_applies_several_rates(self):
        self.assertIsNone(match_class_for_rate(21.0, {4: {21.0, 10.5}}))

    def test_no_match_for_unknown_or_missing_rate(self):
        self.assertIsNone(match_class_for_rate(27.0, {4: {21.0}}))
        self.assertIsNone(match_class_for_rate(None, {4: {21.0}}))
        self.assertIsNone(match_class_for_rate(21.0, {}))
