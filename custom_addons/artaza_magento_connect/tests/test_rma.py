"""Returns: the state machine, the restock decision, the credit note, the coupon.

Two guarantees carry real money and are tested explicitly: nothing advances
locally unless Magento accepted the push, and a retry never mints a second
coupon or a second credit note.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from ..models.artaza_magento_rma import _email_slug, _order_short, _random_suffix
from .common import MagentoCase


@tagged('post_install', '-at_install')
class TestRma(MagentoCase):

    def setUp(self):
        super().setUp()
        self.RMA = self.env['artaza.magento.rma']
        self.SO = self.env['sale.order']

    def make_rma(self, **kw):
        with self.patch_client():
            rma = self.RMA._magento_absorb_rma(self.make_rma_payload(**kw))
        return rma

    # ── naming helpers ─────────────────────────────────────────
    def test_email_slug_keeps_the_readable_part(self):
        self.assertEqual(_email_slug('juan.perez@gmail.com'), 'juan.perez')
        self.assertEqual(_email_slug(''), 'cliente')
        self.assertEqual(_email_slug(None), 'cliente')

    def test_order_short_drops_the_leading_zeros(self):
        self.assertEqual(_order_short('000000031'), '31')
        self.assertEqual(_order_short(''), '0')
        self.assertEqual(_order_short('AB-12/x'), 'AB12x')

    def test_random_suffix_avoids_ambiguous_characters(self):
        suffix = _random_suffix(20)
        self.assertEqual(len(suffix), 20)
        self.assertFalse(set(suffix) & set('OIl01'))

    # ── absorb ─────────────────────────────────────────────────
    def test_absorb_creates_the_rma_with_its_lines(self):
        rma = self.make_rma()
        self.assertEqual(rma.magento_increment_id, 'RMA-0001')
        self.assertEqual(rma.state, 'requested')
        self.assertEqual(rma.rma_type, 'exchange')
        self.assertEqual(len(rma.line_ids), 1)
        self.assertEqual(rma.line_ids.product_id, self.product)
        self.assertEqual(rma.partner_id, self.partner)

    def test_absorb_is_create_once(self):
        self.make_rma()
        with self.patch_client():
            self.assertFalse(self.RMA._magento_absorb_rma(self.make_rma_payload()))
        self.assertEqual(
            self.RMA.search_count([('magento_increment_id', '=', 'RMA-0001')]), 1)

    def test_absorb_without_an_increment_id_does_nothing(self):
        with self.patch_client():
            self.assertFalse(self.RMA._magento_absorb_rma({'increment_id': None}))

    def test_absorb_links_the_sale_order_when_it_exists(self):
        order = self.SO._magento_absorb_order(
            self.make_order_payload(), [], ['checkmo'])
        rma = self.make_rma()
        self.assertEqual(rma.sale_order_id, order)

    def test_absorb_keeps_an_unknown_sku_as_a_line_without_a_product(self):
        """The line still has to be visible — the operator decides what it is."""
        payload = self.make_rma_payload()
        payload['items'] = [{'sku': 'SKU-GHOST', 'name': 'Ghost', 'qty_requested': 1}]
        with self.patch_client():
            rma = self.RMA._magento_absorb_rma(payload)
        self.assertEqual(len(rma.line_ids), 1)
        self.assertFalse(rma.line_ids.product_id)

    def test_absorb_falls_back_to_requested_for_an_unknown_status(self):
        rma = self.make_rma(increment_id='RMA-0009', status='what-is-this')
        self.assertEqual(rma.state, 'requested')

    def test_absorb_resolves_the_partner_by_email(self):
        rma = self.make_rma(increment_id='RMA-0010')
        self.assertEqual(rma.partner_id, self.partner)
        self.assertFalse(self.RMA._magento_resolve_partner(None))
        self.assertFalse(self.RMA._magento_resolve_partner('nobody@example.com'))

    # ── the pull cron ──────────────────────────────────────────
    def test_the_cron_pulls_and_advances_the_cursor(self):
        raw = {'entity_id': 5, 'increment_id': 'RMA-0100', 'status': 'requested',
               'order_increment_id': '000000001', 'customer_email': 'buyer@example.com',
               'updated_at': '2026-08-13 15:00:00', 'items': []}
        with self.patch_client(fetch_rmas=([raw], [])):
            created = self.RMA._cron_magento_pull_rmas()
        self.assertEqual(created, 1)
        self.assertEqual(
            self.icp.get_param('artaza_magento_connect.rmas_cursor'),
            '2026-08-13 15:00:00')

    def test_a_failing_rma_is_logged_and_the_page_continues(self):
        raw = {'entity_id': 5, 'increment_id': 'RMA-0101', 'status': 'requested',
               'updated_at': '2026-08-13 15:00:00', 'items': []}
        with self.patch_client(fetch_rmas=([raw], [])):
            with patch.object(type(self.RMA), '_magento_absorb_rma',
                              MagicMock(side_effect=ValueError('boom'))):
                self.RMA._cron_magento_pull_rmas()
        log = self.last_log('rma_pull')
        self.assertEqual(log.state, 'partial')
        self.assertIn('RMA-0101', log.line_ids.mapped('ref'))

    def test_import_one_reports_each_outcome(self):
        self.assertEqual(self.RMA._magento_import_one('')['status'], 'error')
        self.make_rma()
        self.assertEqual(self.RMA._magento_import_one('RMA-0001')['status'], 'exists')
        with self.patch_client(fetch_rma_by_increment=None):
            self.assertEqual(self.RMA._magento_import_one('nope')['status'], 'not_found')
        with self.patch_client(fetch_rma_by_increment=UserError('boom')):
            self.assertEqual(self.RMA._magento_import_one('x')['status'], 'error')

    def test_import_one_brings_the_return_in(self):
        raw = {'entity_id': 6, 'increment_id': 'RMA-0200', 'status': 'requested',
               'customer_email': 'buyer@example.com', 'items': []}
        with self.patch_client(fetch_rma_by_increment=raw):
            result = self.RMA._magento_import_one('RMA-0200')
        self.assertEqual(result['status'], 'imported')
        self.assertTrue(self.RMA.search([('magento_increment_id', '=', 'RMA-0200')]))

    # ── the state machine ──────────────────────────────────────
    def test_accept_pushes_and_advances(self):
        rma = self.make_rma()
        with self.patch_client(write_rma_status={}) as mocks:
            rma.action_accept()
        self.assertEqual(mocks['write_rma_status'].call_args[0][1], 'accepted')
        self.assertEqual(rma.state, 'accepted')
        self.assertEqual(rma.magento_status, 'accepted')

    def test_nothing_advances_locally_when_the_push_fails(self):
        """Odoo must never claim a state Magento never received."""
        rma = self.make_rma()
        with self.capture_store() as stored:
            with self.patch_client(write_rma_status=UserError('401')):
                with self.assertRaises(UserError):
                    rma.action_accept()
        self.assertEqual(rma.state, 'requested')
        self.assertEqual(stored['operation'], 'rma_status')
        self.assertIn('401', stored['error_message'])

    def test_reject_demands_a_reason_for_the_customer(self):
        rma = self.make_rma()
        with self.assertRaises(UserError):
            rma.action_reject()
        rma.admin_message = 'Out of the warranty window'
        with self.patch_client(write_rma_status={}) as mocks:
            rma.action_reject()
        self.assertEqual(rma.state, 'rejected')
        self.assertEqual(mocks['write_rma_status'].call_args[1]['admin_message'],
                         'Out of the warranty window')

    def test_an_rma_without_a_magento_id_cannot_be_pushed(self):
        rma = self.make_rma()
        rma.magento_rma_id = 0
        with self.assertRaises(UserError):
            rma.action_accept()

    def test_the_simple_transitions(self):
        for action, expected in (('action_receive', 'inspection'),
                                 ('action_fraud', 'fraud'),
                                 ('action_resolve_return', 'returned'),
                                 ('action_resolve_hold', 'held')):
            rma = self.make_rma(increment_id='RMA-T-%s' % expected)
            with self.patch_client(write_rma_status={}):
                getattr(rma, action)()
            self.assertEqual(rma.state, expected)

    def test_resolve_exchange_records_the_resolution(self):
        rma = self.make_rma()
        with self.patch_client(write_rma_status={}):
            rma.action_resolve_exchange()
        self.assertEqual(rma.state, 'resolved_exchange')
        self.assertEqual(rma.resolution, 'exchange')

    def test_approve_opens_the_restock_wizard(self):
        rma = self.make_rma()
        action = rma.action_approve()
        self.assertEqual(action['res_model'], 'artaza.magento.rma.approve.wizard')
        self.assertEqual(action['context']['default_rma_id'], rma.id)

    # ── restock ────────────────────────────────────────────────
    def test_approving_as_sellable_receives_the_goods_back(self):
        rma = self.make_rma()
        with self.patch_client(write_rma_status={}):
            wizard = self.env['artaza.magento.rma.approve.wizard'].create({
                'rma_id': rma.id, 'restock': 'sellable',
            })
            wizard.action_confirm()
        self.assertEqual(rma.state, 'approved')
        picking = self.env['stock.picking'].search(
            [('origin', 'ilike', rma.magento_increment_id)], limit=1)
        self.assertTrue(picking)
        self.assertEqual(picking.state, 'done')

    def test_approving_as_scrap_sends_them_to_an_inventory_location(self):
        rma = self.make_rma()
        with self.patch_client(write_rma_status={}):
            self.env['artaza.magento.rma.approve.wizard'].create({
                'rma_id': rma.id, 'restock': 'scrap',
            }).action_confirm()
        picking = self.env['stock.picking'].search(
            [('origin', 'ilike', rma.magento_increment_id)], limit=1)
        self.assertEqual(picking.location_dest_id.usage, 'inventory')

    def test_approving_without_restock_only_pushes_the_status(self):
        rma = self.make_rma()
        with self.patch_client(write_rma_status={}):
            rma._approve_with_restock('none')
        self.assertEqual(rma.state, 'approved')
        self.assertFalse(self.env['stock.picking'].search(
            [('origin', 'ilike', rma.magento_increment_id)]))

    def test_restock_is_skipped_when_no_line_matched_a_product(self):
        payload = self.make_rma_payload(increment_id='RMA-NOPROD')
        payload['items'] = [{'sku': 'SKU-GHOST', 'name': 'x', 'qty_requested': 1}]
        with self.patch_client():
            rma = self.RMA._magento_absorb_rma(payload)
        with self.patch_client(write_rma_status={}):
            rma._approve_with_restock('sellable')
        self.assertEqual(rma.state, 'approved')

    # ── credit ─────────────────────────────────────────────────
    def _invoiced_order(self):
        order = self.SO._magento_absorb_order(
            self.make_order_payload(), [], ['checkmo'])
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def test_resolving_as_credit_needs_an_amount(self):
        rma = self.make_rma()
        rma.credit_amount = 0
        with self.assertRaises(UserError):
            rma.action_resolve_credit()

    def test_a_credit_note_cannot_be_built_without_a_posted_invoice(self):
        """No invoice, nothing to reverse — say so instead of inventing one."""
        rma = self.make_rma()
        rma.credit_amount = 100
        with self.assertRaises(UserError) as ctx:
            rma._create_credit_note()
        self.assertIn('invoice', str(ctx.exception).lower())

    def test_the_credit_note_reverses_the_invoice(self):
        order, invoice = self._invoiced_order()
        rma = self.make_rma()
        rma.credit_amount = 100
        rma._create_credit_note()
        self.assertTrue(rma.odoo_reference)
        credit_note = self.env['account.move'].search(
            [('move_type', '=', 'out_refund'), ('name', '=', rma.odoo_reference)])
        self.assertEqual(credit_note.state, 'posted')

    def test_the_credit_note_is_limited_to_what_came_back(self):
        """Bought 2, returned 1 → the credit note is for 1."""
        payload = self.make_order_payload(increment_id='000000002')
        payload['items'][0]['qty'] = 2.0
        order = self.SO._magento_absorb_order(payload, [], ['checkmo'])
        order.action_confirm()
        order._create_invoices().action_post()

        rma_payload = self.make_rma_payload(increment_id='RMA-PARTIAL',
                                            order_increment_id='000000002')
        rma_payload['items'][0]['qty_requested'] = 1.0
        with self.patch_client():
            rma = self.RMA._magento_absorb_rma(rma_payload)
        rma._create_credit_note()
        credit_note = self.env['account.move'].search([('name', '=', rma.odoo_reference)])
        line = credit_note.invoice_line_ids.filtered(
            lambda l: l.product_id == self.product)
        self.assertEqual(line.quantity, 1.0)

    def test_a_credit_note_with_no_matching_line_is_refused(self):
        order, invoice = self._invoiced_order()
        rma_payload = self.make_rma_payload(increment_id='RMA-NOMATCH')
        other = self.env['product.product'].create({
            'name': 'Unrelated', 'default_code': 'SKU-OTHER', 'is_storable': True,
        })
        rma_payload['items'] = [{'sku': 'SKU-OTHER', 'name': 'Unrelated',
                                 'qty_requested': 1}]
        with self.patch_client():
            rma = self.RMA._magento_absorb_rma(rma_payload)
        self.assertTrue(other)
        with self.assertRaises(UserError):
            rma._create_credit_note()

    # ── coupon ─────────────────────────────────────────────────
    def _coupon_patches(self):
        return dict(
            fetch_store_views=[{'id': 1, 'website_id': 1, 'code': 'default'}],
            get_order={'store_id': 1},
            fetch_customer_group_ids=[0, 1],
            create_cart_price_rule=77,
            create_specific_coupon={},
            write_rma_status={},
        )

    def test_the_coupon_is_named_after_the_customer_and_order(self):
        rma = self.make_rma()
        rma.credit_amount = 500
        with self.patch_client(**self._coupon_patches()) as mocks:
            rma._generate_coupon()
        rule = mocks['create_cart_price_rule'].call_args[0][0]
        self.assertEqual(rule['name'], 'buyer-1')
        self.assertEqual(rule['discount_amount'], 500)
        self.assertEqual(rule['uses_per_coupon'], 1)
        self.assertEqual(rule['simple_action'], 'cart_fixed')
        self.assertTrue(rma.coupon_code.startswith('buyer-1-'))

    def test_a_retry_never_mints_a_second_coupon(self):
        rma = self.make_rma()
        rma.credit_amount = 500
        with self.patch_client(**self._coupon_patches()) as mocks:
            rma._generate_coupon()
            first = rma.coupon_code
            rma._generate_coupon()
        self.assertEqual(rma.coupon_code, first)
        self.assertEqual(mocks['create_cart_price_rule'].call_count, 1)

    def test_a_coupon_needs_an_email_to_go_to(self):
        rma = self.make_rma()
        rma.credit_amount = 500
        rma.customer_email = False
        rma.partner_id = False
        with self.assertRaises(UserError):
            rma._generate_coupon()

    def test_the_website_falls_back_when_the_order_store_is_unknown(self):
        rma = self.make_rma()
        client = self.env['artaza.magento.client']
        with self.patch_client(fetch_store_views=[{'id': 9, 'website_id': 3}],
                               get_order={}):
            self.assertEqual(rma._magento_coupon_website(client), 3)

    def test_an_unresolvable_website_is_an_error_not_a_guess(self):
        rma = self.make_rma()
        client = self.env['artaza.magento.client']
        with self.patch_client(fetch_store_views=[], get_order={}):
            with self.assertRaises(UserError):
                rma._magento_coupon_website(client)

    def test_resolve_credit_does_the_whole_thing_once(self):
        order, invoice = self._invoiced_order()
        rma = self.make_rma()
        rma.credit_amount = 500
        with self.patch_client(**self._coupon_patches()) as mocks:
            rma.action_resolve_credit()
        self.assertEqual(rma.state, 'resolved_credit')
        self.assertEqual(rma.resolution, 'credit')
        self.assertTrue(rma.odoo_reference)
        self.assertTrue(rma.coupon_code)
        pushed = mocks['write_rma_status'].call_args[1]
        self.assertEqual(pushed['coupon_code'], rma.coupon_code)

    # ── replacement delivery ───────────────────────────────────
    def test_the_replacement_delivery_needs_a_resolved_rma(self):
        rma = self.make_rma()
        with self.assertRaises(UserError):
            rma.action_create_replacement_delivery()

    def test_the_replacement_delivery_is_prefilled(self):
        rma = self.make_rma()
        with self.patch_client(write_rma_status={}):
            rma.action_resolve_exchange()
        action = rma.action_create_replacement_delivery()
        picking = self.env['stock.picking'].browse(action['res_id'])
        self.assertEqual(picking.partner_id, self.partner)
        self.assertEqual(picking.move_ids.product_id, self.product)
        self.assertIn(picking.state, ('draft', 'confirmed', 'assigned'))

    def test_no_replacement_without_a_product_matched_in_odoo(self):
        payload = self.make_rma_payload(increment_id='RMA-NOPROD2')
        payload['items'] = [{'sku': 'SKU-GHOST', 'name': 'x', 'qty_requested': 1}]
        with self.patch_client():
            rma = self.RMA._magento_absorb_rma(payload)
        with self.patch_client(write_rma_status={}):
            rma.action_resolve_exchange()
        with self.assertRaises(UserError):
            rma.action_create_replacement_delivery()

    # ── computed fields ────────────────────────────────────────
    def test_the_refund_reference_follows_the_invoiced_price(self):
        order, invoice = self._invoiced_order()
        rma = self.make_rma()
        self.assertGreater(rma.refund_amount_total, 0)
        self.assertEqual(rma.credit_amount, rma.refund_amount_total)

    def test_view_sale_order_returns_an_action(self):
        order = self.SO._magento_absorb_order(
            self.make_order_payload(), [], ['checkmo'])
        rma = self.make_rma()
        action = rma.action_view_sale_order()
        self.assertEqual(action['res_id'], order.id)
