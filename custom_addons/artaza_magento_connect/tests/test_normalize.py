"""The payload readers — pure functions, so they get tested exhaustively.

Reading Magento's nested payloads is where a connector silently loses data:
a configurable that hides the real SKU, a street that is a list, freight that
comes net instead of gross. Each of those has a case here.
"""
from odoo.tests.common import TransactionCase, tagged

from ..models.magento_normalize import (
    extract_items,
    is_fully_invoiced,
    is_fully_shipped,
    normalize_order,
    normalize_rma,
)


@tagged('post_install', '-at_install')
class TestNormalize(TransactionCase):

    # ── extract_items ──────────────────────────────────────────
    def test_simple_item(self):
        items = extract_items([{
            'sku': 'A1', 'name': 'Phone', 'qty_ordered': 2, 'price_incl_tax': 121.0,
        }])
        self.assertEqual(items, [{'sku': 'A1', 'name': 'Phone', 'qty': 2, 'price': 121.0}])

    def test_configurable_takes_child_sku_and_parent_price(self):
        """The parent carries price and qty, the child carries the stocked SKU."""
        items = extract_items([
            {'item_id': 1, 'sku': 'PARENT', 'name': 'Phone', 'product_type': 'configurable',
             'qty_ordered': 3, 'price_incl_tax': 200.0},
            {'item_id': 2, 'sku': 'CHILD-RED', 'name': 'Phone Red',
             'parent_item_id': 1, 'qty_ordered': 3, 'price_incl_tax': 0.0},
        ])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['sku'], 'CHILD-RED')
        self.assertEqual(items[0]['price'], 200.0)
        self.assertEqual(items[0]['qty'], 3)

    def test_configurable_without_child_falls_back_to_parent(self):
        items = extract_items([
            {'item_id': 1, 'sku': 'PARENT', 'product_type': 'configurable',
             'qty_ordered': 1, 'price_incl_tax': 50.0},
        ])
        self.assertEqual(items[0]['sku'], 'PARENT')

    def test_item_without_sku_is_dropped(self):
        self.assertEqual(extract_items([{'name': 'ghost', 'qty_ordered': 1}]), [])

    def test_price_falls_back_to_net_when_gross_missing(self):
        items = extract_items([{'sku': 'A1', 'qty_ordered': 1, 'price': 99.0}])
        self.assertEqual(items[0]['price'], 99.0)

    def test_empty_and_none(self):
        self.assertEqual(extract_items([]), [])
        self.assertEqual(extract_items(None), [])

    # ── normalize_order ────────────────────────────────────────
    def test_normalize_order_flattens_the_payload(self):
        order = normalize_order({
            'increment_id': '000000010', 'entity_id': 7, 'state': 'processing',
            'status': 'processing', 'grand_total': 121.0,
            'shipping_incl_tax': 12.1, 'shipping_amount': 10.0,
            'order_currency_code': 'USD', 'customer_email': 'a@b.com',
            'customer_is_guest': 1,
            'payment': {'method': 'checkmo'},
            'billing_address': {'firstname': 'Jo', 'lastname': 'Doe',
                                'street': ['Main 1', 'Apt 2'], 'city': 'Rosario'},
            'extension_attributes': {
                'afip_responsibility': '5',
                'shipping_assignments': [
                    {'shipping': {'address': {'city': 'Córdoba', 'street': 'Other 5'}}},
                ],
            },
            'items': [{'sku': 'A1', 'qty_ordered': 1, 'price_incl_tax': 121.0}],
        })
        self.assertEqual(order['increment_id'], '000000010')
        self.assertEqual(order['payment_method'], 'checkmo')
        self.assertTrue(order['customer']['is_guest'])
        self.assertEqual(order['customer']['firstname'], 'Jo')
        self.assertEqual(order['customer']['afip_responsibility'], '5')
        # gross freight wins over net
        self.assertEqual(order['shipping_amount'], 12.1)
        # a list of street parts becomes one string
        self.assertEqual(order['billing']['street'], 'Main 1, Apt 2')
        # the shipping address lives under extension_attributes
        self.assertEqual(order['shipping']['city'], 'Córdoba')
        self.assertEqual(order['shipping']['street'], 'Other 5')

    def test_normalize_order_without_shipping_assignment(self):
        order = normalize_order({'increment_id': 'x', 'items': []})
        self.assertEqual(order['shipping']['city'], None)
        self.assertEqual(order['items'], [])

    def test_normalize_order_falls_back_to_net_freight(self):
        order = normalize_order({'increment_id': 'x', 'shipping_amount': 10.0, 'items': []})
        self.assertEqual(order['shipping_amount'], 10.0)

    # ── normalize_rma ──────────────────────────────────────────
    def test_normalize_rma(self):
        rma = normalize_rma({
            'entity_id': 42, 'increment_id': 'RMA-1', 'order_increment_id': '000000010',
            'status': 'requested', 'type': 'exchange', 'customer_email': 'a@b.com',
            'items': [
                {'sku': 'A1', 'name': 'Phone', 'qty_requested': 2, 'order_item_id': 9},
                {'name': 'no sku, dropped'},
            ],
        })
        self.assertEqual(rma['rma_id'], 42)
        self.assertEqual(rma['increment_id'], 'RMA-1')
        self.assertEqual(len(rma['items']), 1)
        self.assertEqual(rma['items'][0]['order_item_id'], 9)

    def test_normalize_rma_without_items(self):
        self.assertEqual(normalize_rma({'entity_id': 1})['items'], [])

    # ── fulfillment predicates ─────────────────────────────────
    def test_is_fully_invoiced(self):
        self.assertTrue(is_fully_invoiced({'grand_total': 100, 'total_invoiced': 100}))
        self.assertFalse(is_fully_invoiced({'grand_total': 100, 'total_invoiced': 40}))
        self.assertFalse(is_fully_invoiced({'grand_total': 0, 'total_invoiced': 0}))
        self.assertFalse(is_fully_invoiced({}))

    def test_is_fully_shipped(self):
        self.assertTrue(is_fully_shipped({'items': [
            {'qty_ordered': 2, 'qty_shipped': 2},
        ]}))
        self.assertFalse(is_fully_shipped({'items': [
            {'qty_ordered': 2, 'qty_shipped': 1},
        ]}))

    def test_is_fully_shipped_skips_children_and_virtuals(self):
        self.assertTrue(is_fully_shipped({'items': [
            {'qty_ordered': 1, 'qty_shipped': 1},
            {'qty_ordered': 1, 'qty_shipped': 0, 'parent_item_id': 5},
            {'qty_ordered': 1, 'qty_shipped': 0, 'is_virtual': True},
        ]}))

    def test_is_fully_shipped_is_false_when_nothing_is_shippable(self):
        """Nothing to ship is a no-op, not 'done' — otherwise a virtual-only
        order would be reported as shipped."""
        self.assertFalse(is_fully_shipped({'items': []}))
        self.assertFalse(is_fully_shipped({'items': [{'is_virtual': True, 'qty_ordered': 1}]}))
        self.assertFalse(is_fully_shipped({'items': [{'qty_ordered': 0}]}))
