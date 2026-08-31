"""Order import, negotiation and fulfillment.

The rule the guard tests defend: an order is imported **whole or not at all**.
A half-imported order carries a total that no longer matches Magento's, and
nothing on screen says why — the exact class of silent bug this module exists
to eliminate.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from ..models.sale_order import _split_methods
from .common import MagentoCase


@tagged('post_install', '-at_install')
class TestOrders(MagentoCase):

    def setUp(self):
        super().setUp()
        self.SO = self.env['sale.order']
        self.paid = ['mercadopago']
        self.pending = ['checkmo', 'banktransfer']

    def absorb(self, payload=None, **kw):
        return self.SO._magento_absorb_order(
            payload or self.make_order_payload(), self.paid, self.pending, **kw)

    # ── helpers ────────────────────────────────────────────────
    def test_split_methods(self):
        self.assertEqual(_split_methods('a, b ,c'), ['a', 'b', 'c'])
        self.assertEqual(_split_methods(''), [])
        self.assertEqual(_split_methods(None), [])

    # ── payment-method routing ─────────────────────────────────
    def test_pending_offline_order_becomes_a_quotation(self):
        order = self.absorb()
        self.assertEqual(order.state, 'draft')
        self.assertEqual(order.magento_order_id, '000000001')
        self.assertEqual(order.magento_order_total, 1210.0)
        self.assertEqual(len(order.order_line), 1)

    def test_paid_order_is_confirmed(self):
        payload = self.make_order_payload(state='processing', payment_method='mercadopago')
        order = self.absorb(payload)
        self.assertEqual(order.state, 'sale')

    def test_unlisted_payment_method_is_not_absorbed(self):
        payload = self.make_order_payload(payment_method='paypal_unknown')
        self.assertIsNone(self.absorb(payload))

    def test_force_absorbs_whatever_the_method(self):
        payload = self.make_order_payload(payment_method='paypal_unknown')
        self.assertTrue(self.absorb(payload, force=True))

    def test_create_once_returns_the_existing_order(self):
        first = self.absorb()
        again = self.absorb()
        self.assertEqual(first, again)
        self.assertEqual(
            self.SO.search_count([('magento_order_id', '=', '000000001')]), 1)

    # ── the integrity guard ────────────────────────────────────
    def test_an_unknown_sku_refuses_the_whole_order(self):
        payload = self.make_order_payload()
        payload['items'].append({'sku': 'SKU-GHOST', 'name': 'ghost',
                                 'qty': 1.0, 'price': 10.0})
        with self.assertRaises(UserError) as ctx:
            self.absorb(payload)
        self.assertIn('SKU-GHOST', str(ctx.exception))
        self.assertFalse(self.SO.search([('magento_order_id', '=', '000000001')]))

    def test_a_refused_order_leaves_no_partner_behind(self):
        """The guard runs before the customer upsert, so nothing is created."""
        payload = self.make_order_payload()
        payload['customer']['email'] = 'brand-new@example.com'
        payload['items'] = [{'sku': 'SKU-GHOST', 'name': 'x', 'qty': 1, 'price': 1}]
        before = self.env['res.partner'].search_count([])
        with self.assertRaises(UserError):
            self.absorb(payload)
        self.assertEqual(self.env['res.partner'].search_count([]), before)

    def test_the_guard_lists_every_missing_sku_once(self):
        payload = self.make_order_payload()
        payload['items'] = [
            {'sku': 'GHOST-A', 'name': 'x', 'qty': 1, 'price': 1},
            {'sku': 'GHOST-A', 'name': 'x', 'qty': 1, 'price': 1},
            {'sku': 'GHOST-B', 'name': 'x', 'qty': 1, 'price': 1},
        ]
        with self.assertRaises(UserError) as ctx:
            self.absorb(payload)
        message = str(ctx.exception)
        self.assertEqual(message.count('GHOST-A'), 1)
        self.assertIn('GHOST-B', message)

    # ── totals, shipping and taxes ─────────────────────────────
    def test_the_magento_price_wins_over_the_pricelist(self):
        payload = self.make_order_payload()
        payload['items'][0]['price'] = 999.0
        order = self.absorb(payload)
        self.assertEqual(order.order_line[0].price_unit, 999.0)

    def test_shipping_arrives_as_its_own_line(self):
        payload = self.make_order_payload(shipping_amount=50.0)
        order = self.absorb(payload)
        shipping = order.order_line.filtered(
            lambda line: line.product_id.default_code == 'MAGENTO_SHIPPING')
        self.assertEqual(len(shipping), 1)
        self.assertEqual(shipping.price_unit, 50.0)
        self.assertTrue(shipping.tax_ids, "the freight line must carry a tax")

    def test_the_shipping_product_is_created_once_and_reused(self):
        first = self.SO._magento_shipping_product()
        self.assertEqual(first.type, 'service')
        self.assertEqual(first, self.SO._magento_shipping_product())

    def test_the_configured_shipping_tax_wins(self):
        other = self.env['account.tax'].create({
            'name': 'Freight 21%', 'amount': 21.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'country_id': self.tax.country_id.id,
        })
        self.icp.set_param('artaza_magento_connect.shipping_tax_id', str(other.id))
        order = self.absorb(self.make_order_payload(shipping_amount=10.0))
        shipping = order.order_line.filtered(
            lambda line: line.product_id.default_code == 'MAGENTO_SHIPPING')
        self.assertEqual(shipping.tax_ids, other)

    def test_shipping_tax_falls_back_to_the_products_tax(self):
        self.icp.set_param('artaza_magento_connect.shipping_tax_id', '')
        order = self.absorb(self.make_order_payload(shipping_amount=10.0))
        shipping = order.order_line.filtered(
            lambda line: line.product_id.default_code == 'MAGENTO_SHIPPING')
        self.assertEqual(shipping.tax_ids, self.tax)

    def test_a_stale_configured_shipping_tax_is_ignored(self):
        self.icp.set_param('artaza_magento_connect.shipping_tax_id', '999999')
        order = self.absorb(self.make_order_payload(shipping_amount=10.0))
        self.assertTrue(order.order_line.filtered(
            lambda line: line.product_id.default_code == 'MAGENTO_SHIPPING').tax_ids)

    # ── the customer ───────────────────────────────────────────
    def test_a_known_email_reuses_the_partner(self):
        order = self.absorb()
        self.assertEqual(order.partner_id, self.partner)

    def test_an_unknown_customer_is_created_with_its_address(self):
        payload = self.make_order_payload()
        payload['customer'].update(email='new@example.com',
                                   firstname=None, lastname=None)
        payload['billing'] = {
            'firstname': 'Ada', 'lastname': 'Lovelace', 'street': 'Main 1',
            'city': 'Rosario', 'postcode': 'S2000', 'telephone': '+54 1',
            'country_id': 'AR', 'vat_id': '20-1234-5',
        }
        order = self.absorb(payload)
        self.assertEqual(order.partner_id.name, 'Ada Lovelace')
        self.assertEqual(order.partner_id.city, 'Rosario')
        self.assertEqual(order.partner_id.country_id.code, 'AR')
        self.assertEqual(order.partner_id.vat, '20-1234-5')

    def test_a_customer_without_a_name_still_gets_one(self):
        payload = self.make_order_payload()
        payload['customer'].update(email='noname@example.com',
                                   firstname=None, lastname=None)
        payload['billing'] = {}
        self.assertEqual(self.absorb(payload).partner_id.name, 'noname@example.com')

    def test_fiscal_values_are_empty_without_a_localisation(self):
        """Country-agnostic by default: no l10n installed, no fiscal fields."""
        if 'l10n_ar.afip.responsibility.type' in self.env:
            self.skipTest('l10n_ar installed: the AR mapping is exercised instead')
        self.assertEqual(self.SO._magento_fiscal_partner_values('monotributo'), {})

    def test_updating_fiscal_data_never_blocks_the_import(self):
        """A refresh that blows up must not cost you the order."""
        payload = self.make_order_payload()
        payload['customer']['afip_responsibility'] = 'monotributo'
        with patch.object(type(self.SO), '_magento_fiscal_partner_values',
                          MagicMock(return_value={'vat': '20-9-9'})), \
             patch.object(type(self.env['res.partner']), 'write',
                          MagicMock(side_effect=ValueError('boom'))):
            order = self.absorb(payload)
        self.assertTrue(order)

    # ── the cron ───────────────────────────────────────────────
    def test_the_cron_absorbs_a_page_and_advances_the_cursor(self):
        self.icp.set_param('artaza_magento_connect.processing_methods', 'mercadopago')
        self.icp.set_param('artaza_magento_connect.pending_methods', 'checkmo')
        raw = {'increment_id': '000000077', 'entity_id': 1, 'state': 'new',
               'updated_at': '2026-08-13 12:00:00', 'grand_total': 1210.0,
               'payment': {'method': 'checkmo'}, 'customer_email': 'buyer@example.com',
               'items': [{'sku': 'SKU-PHONE', 'qty_ordered': 1, 'price_incl_tax': 1210.0}]}
        with self.patch_client(fetch_orders=([raw], [])):
            self.SO._cron_magento_pull_orders()
        self.assertTrue(self.SO.search([('magento_order_id', '=', '000000077')]))
        self.assertEqual(
            self.icp.get_param('artaza_magento_connect.orders_cursor'),
            '2026-08-13 12:00:00')

    def test_a_failing_order_is_logged_and_the_page_continues(self):
        self.icp.set_param('artaza_magento_connect.pending_methods', 'checkmo')
        good = {'increment_id': '000000080', 'entity_id': 1, 'state': 'new',
                'updated_at': '2026-08-13 12:00:00', 'grand_total': 10.0,
                'payment': {'method': 'checkmo'}, 'customer_email': 'buyer@example.com',
                'items': [{'sku': 'SKU-PHONE', 'qty_ordered': 1, 'price_incl_tax': 10.0}]}
        bad = dict(good, increment_id='000000081',
                   items=[{'sku': 'SKU-GHOST', 'qty_ordered': 1, 'price_incl_tax': 10.0}])
        with self.patch_client(fetch_orders=([good, bad], [])):
            self.SO._cron_magento_pull_orders()
        self.assertTrue(self.SO.search([('magento_order_id', '=', '000000080')]))
        self.assertFalse(self.SO.search([('magento_order_id', '=', '000000081')]))
        log = self.last_log('order_pull')
        self.assertEqual(log.state, 'partial')
        self.assertIn('000000081', log.line_ids.mapped('ref'))

    def test_the_cron_stops_on_an_empty_page(self):
        with self.patch_client(fetch_orders=([],)) as mocks:
            self.SO._cron_magento_pull_orders()
        self.assertEqual(mocks['fetch_orders'].call_count, 1)

    # ── import one by number ───────────────────────────────────
    def test_import_one_reports_each_outcome(self):
        self.assertEqual(self.SO._magento_import_one('')['status'], 'error')

        self.absorb()
        self.assertEqual(self.SO._magento_import_one('000000001')['status'], 'exists')

        with self.patch_client(fetch_order_by_increment=None):
            self.assertEqual(self.SO._magento_import_one('nope')['status'], 'not_found')

        with self.patch_client(fetch_order_by_increment=UserError('boom')):
            self.assertEqual(self.SO._magento_import_one('x')['status'], 'error')

    def test_import_one_bypasses_the_payment_method_gate(self):
        raw = {'increment_id': '000000090', 'entity_id': 2, 'state': 'new',
               'updated_at': '2026-08-13 12:00:00', 'grand_total': 10.0,
               'payment': {'method': 'not-configured'},
               'customer_email': 'buyer@example.com',
               'items': [{'sku': 'SKU-PHONE', 'qty_ordered': 1, 'price_incl_tax': 10.0}]}
        with self.patch_client(fetch_order_by_increment=raw):
            result = self.SO._magento_import_one('000000090')
        self.assertEqual(result['status'], 'imported')

    def test_import_one_surfaces_an_absorption_error(self):
        raw = {'increment_id': '000000091', 'entity_id': 2, 'state': 'new',
               'updated_at': '2026-08-13 12:00:00', 'grand_total': 10.0,
               'payment': {'method': 'checkmo'}, 'customer_email': 'buyer@example.com',
               'items': [{'sku': 'SKU-GHOST', 'qty_ordered': 1, 'price_incl_tax': 10.0}]}
        with self.patch_client(fetch_order_by_increment=raw):
            result = self.SO._magento_import_one('000000091')
        self.assertEqual(result['status'], 'error')
        self.assertIn('SKU-GHOST', result['message'])

    # ── the import wizard ──────────────────────────────────────
    def test_the_wizard_reports_and_can_open_the_order(self):
        order = self.absorb()
        wizard = self.env['artaza.magento.order.import.wizard'].create(
            {'order_number': '000000001'})
        wizard.action_import()
        self.assertEqual(wizard.state, 'exists')
        self.assertEqual(wizard.sale_order_id, order)
        self.assertIn(order.name, wizard.message)
        action = wizard.action_view_order()
        self.assertEqual(action['res_id'], order.id)

    def test_the_wizard_reports_a_missing_order(self):
        wizard = self.env['artaza.magento.order.import.wizard'].create(
            {'order_number': 'nope'})
        with self.patch_client(fetch_order_by_increment=None):
            wizard.action_import()
        self.assertEqual(wizard.state, 'not_found')
        self.assertFalse(wizard.sale_order_id)

    # ── negotiation ────────────────────────────────────────────
    def test_negotiation_pushes_the_adjustment_and_logs_it(self):
        order = self.absorb()
        order.magento_adjustment_reason = 'agreed discount'
        with self.patch_client(write_order_negotiation={}) as mocks:
            order.action_magento_push_negotiation()
        mocks['write_order_negotiation'].assert_called_once()
        log = self.last_log('negotiation')
        self.assertEqual(log.state, 'success')

    def test_negotiation_demands_a_reason(self):
        order = self.absorb()
        with self.assertRaises(UserError):
            order.action_magento_push_negotiation()

    def test_negotiation_refuses_a_non_magento_order(self):
        order = self.SO.create({'partner_id': self.partner.id})
        order.magento_adjustment_reason = 'x'
        with self.assertRaises(UserError):
            order.action_magento_push_negotiation()

    def test_the_total_wizard_lands_exactly_on_the_typed_total(self):
        """Odoo only computes forward; this back-calculates and must not drift."""
        order = self.absorb()
        wizard = self.env['artaza.magento.order.total.wizard'].create({
            'order_id': order.id, 'new_total': 1000.0, 'reason': 'agreed',
        })
        wizard.action_apply()
        self.assertEqual(order.magento_adjustment_reason, 'agreed')
        self.assertAlmostEqual(order.amount_total, 1000.0, places=2)

    def test_the_total_wizard_refuses_impossible_input(self):
        order = self.absorb()
        wizard = self.env['artaza.magento.order.total.wizard'].create({
            'order_id': order.id, 'new_total': 0.0, 'reason': 'x',
        })
        with self.assertRaises(UserError):
            wizard.action_apply()

    def test_the_total_wizard_refuses_an_order_with_no_priced_lines(self):
        empty = self.SO.create({'partner_id': self.partner.id})
        wizard = self.env['artaza.magento.order.total.wizard'].create({
            'order_id': empty.id, 'new_total': 100.0, 'reason': 'x',
        })
        with self.assertRaises(UserError):
            wizard.action_apply()

    def test_open_set_total_wizard_returns_an_action(self):
        order = self.absorb()
        action = order.action_open_set_total_wizard()
        self.assertEqual(action['res_model'], 'artaza.magento.order.total.wizard')

    # ── fulfillment ────────────────────────────────────────────
    def test_validating_the_delivery_invoices_and_ships_in_magento(self):
        payload = self.make_order_payload(state='processing',
                                          payment_method='mercadopago')
        order = self.absorb(payload)
        picking = order.picking_ids[:1]
        if not picking:
            self.skipTest('no delivery generated in this configuration')
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        with self.patch_client(
            get_order={'grand_total': 1210.0, 'total_invoiced': 0.0,
                       'items': [{'qty_ordered': 1, 'qty_shipped': 0}]},
            create_invoice=1, create_shipment=2,
        ) as mocks:
            picking._action_done()
        mocks['create_invoice'].assert_called_once()
        mocks['create_shipment'].assert_called_once()

    def test_a_picking_without_a_magento_order_pushes_nothing(self):
        order = self.SO.create({
            'partner_id': self.partner.id,
            'order_line': [(0, 0, {'product_id': self.product.id,
                                   'product_uom_qty': 1})],
        })
        order.action_confirm()
        picking = order.picking_ids[:1]
        if not picking:
            self.skipTest('no delivery generated in this configuration')
        with self.patch_client(get_order={}) as mocks:
            picking._magento_push_shipment()
        mocks['get_order'].assert_not_called()
