"""The three outbound pushes, and the dirty flags that schedule them.

The recurring rule: **never write a guess**. A warehouse with no source and a
tax with no class are skipped and reported, not approximated — writing the wrong
Magento source or tax class is invisible until an accountant finds it.
"""
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from .common import MagentoCase


@tagged('post_install', '-at_install')
class TestPushes(MagentoCase):

    def setUp(self):
        super().setUp()
        self.Product = self.env['product.product']

    # ── dirty flags ────────────────────────────────────────────
    def test_changing_the_price_flags_the_product(self):
        self.product.magento_price_dirty = False
        self.product.product_tmpl_id.list_price = 1500.0
        self.assertTrue(self.product.magento_price_dirty)

    def test_changing_the_tax_flags_the_product(self):
        self.product.magento_tax_dirty = False
        self.product.product_tmpl_id.taxes_id = [(6, 0, self.tax.ids)]
        self.assertTrue(self.product.magento_tax_dirty)

    def test_an_unrelated_write_flags_nothing(self):
        self.product.magento_price_dirty = False
        self.product.product_tmpl_id.name = 'Renamed'
        self.assertFalse(self.product.magento_price_dirty)

    def test_a_stock_move_flags_the_product(self):
        self.product.magento_stock_dirty = False
        self.env['stock.quant'].with_context(inventory_mode=True).create({
            'product_id': self.product.id,
            'location_id': self.warehouse.lot_stock_id.id,
            'inventory_quantity': 7,
        }).action_apply_inventory()
        self.assertTrue(self.product.magento_stock_dirty)

    def test_mark_all_dirty(self):
        self.product.magento_stock_dirty = False
        count = self.Product.magento_mark_all_dirty()
        self.assertGreaterEqual(count, 1)
        self.assertTrue(self.product.magento_stock_dirty)

    # ── stock ──────────────────────────────────────────────────
    def test_stock_is_pushed_per_source(self):
        with self.patch_client(write_source_items={}) as mocks:
            result = self.Product._magento_push_stock(self.product)
        payload = mocks['write_source_items'].call_args[0][0]
        self.assertEqual(payload[0]['sku'], 'SKU-PHONE')
        self.assertEqual(payload[0]['source_code'], 'default')
        self.assertEqual(result['skus'], 1)
        self.assertFalse(result['failures'])

    def test_two_warehouses_on_one_source_are_summed(self):
        """MSI holds one quantity per (sku, source): they must arrive added up,
        not overwriting each other."""
        second = self.env['stock.warehouse'].create({
            'name': 'Second', 'code': 'WH2', 'magento_source_id': self.source.id,
        })
        for warehouse, qty in ((self.warehouse, 5), (second, 3)):
            self.env['stock.quant'].with_context(inventory_mode=True).create({
                'product_id': self.product.id,
                'location_id': warehouse.lot_stock_id.id,
                'inventory_quantity': qty,
            }).action_apply_inventory()
        with self.patch_client(write_source_items={}) as mocks:
            self.Product._magento_push_stock(self.product)
        payload = mocks['write_source_items'].call_args[0][0]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]['quantity'], 8)
        self.assertEqual(payload[0]['status'], 1)

    def test_a_warehouse_without_a_source_is_reported_once(self):
        self.env['stock.warehouse'].create({'name': 'Orphan', 'code': 'ORPH'})
        with self.patch_client(write_source_items={}):
            result = self.Product._magento_push_stock(self.product)
        self.assertEqual(len(result['failures']), 1)
        self.assertEqual(result['failures'][0]['ref'], 'ORPH')
        self.assertIn('no Magento source', result['failures'][0]['reason'])

    def test_zero_stock_is_sent_as_out_of_stock(self):
        with self.patch_client(write_source_items={}) as mocks:
            self.Product._magento_push_stock(self.product)
        self.assertEqual(mocks['write_source_items'].call_args[0][0][0]['status'], 0)

    # ── price ──────────────────────────────────────────────────
    def test_price_is_pushed_at_the_global_scope(self):
        with self.patch_client(write_base_prices=[]) as mocks:
            result = self.Product._magento_push_price(self.product)
        payload = mocks['write_base_prices'].call_args[0][0]
        self.assertEqual(payload[0]['price'], 1210.0)
        self.assertEqual(payload[0]['store_id'], 0)
        self.assertEqual(result['written'], 1)

    def test_a_per_item_price_error_is_not_read_as_success(self):
        """Magento answers 200 with an error array; that is a partial failure."""
        with self.patch_client(write_base_prices=[{'sku': 'SKU-PHONE',
                                                   'message': 'unknown sku'}]):
            result = self.Product._magento_push_price(self.product)
        self.assertEqual(result['written'], 0)
        self.assertEqual(len(result['failures']), 1)
        self.assertEqual(result['failures'][0]['ref'], 'SKU-PHONE')
        self.assertNotIn('SKU-PHONE', result['ok_skus'])

    # ── tax class ──────────────────────────────────────────────
    def test_tax_class_is_pushed_grouped_by_class(self):
        with self.patch_client(write_product_tax_class=[]) as mocks:
            result = self.Product._magento_push_tax(self.product)
        args = mocks['write_product_tax_class'].call_args[0]
        self.assertEqual(args[0], 4)                 # the Magento class id
        self.assertEqual(args[1], ['SKU-PHONE'])
        self.assertFalse(result['failures'])

    def test_a_tax_without_a_class_is_skipped_and_reported(self):
        self.tax.magento_tax_class_id = False
        with self.patch_client(write_product_tax_class=[]) as mocks:
            result = self.Product._magento_push_tax(self.product)
        mocks['write_product_tax_class'].assert_not_called()
        self.assertEqual(len(result['failures']), 1)

    def test_a_product_without_a_sale_tax_is_skipped_and_reported(self):
        self.product.taxes_id = [(5, 0, 0)]
        with self.patch_client(write_product_tax_class=[]):
            result = self.Product._magento_push_tax(self.product)
        self.assertEqual(len(result['failures']), 1)

    def test_skus_magento_did_not_find_come_back_as_failures(self):
        with self.patch_client(write_product_tax_class=['SKU-PHONE']):
            result = self.Product._magento_push_tax(self.product)
        self.assertEqual(len(result['failures']), 1)

    def test_the_first_sale_tax_wins_when_a_product_has_several(self):
        other = self.env['account.tax'].create({
            'name': 'VAT 10.5% (test)', 'amount': 10.5, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'country_id': self.tax.country_id.id,
        })
        self.product.taxes_id = [(6, 0, (self.tax + other).ids)]
        self.assertEqual(len(self.product._magento_sale_tax()), 1)

    # ── the cron and the manual button ─────────────────────────
    def test_the_cron_pushes_and_clears_the_flag(self):
        self.product.magento_stock_dirty = True
        with self.patch_client(write_source_items={}):
            self.Product._cron_magento_sync_stock()
        self.assertFalse(self.product.magento_stock_dirty)
        log = self.last_log('stock')
        self.assertIn(log.state, ('success', 'partial'))

    def test_a_failing_cron_push_keeps_the_flag_and_records_the_error(self):
        self.product.magento_stock_dirty = True
        with self.patch_client(write_source_items=UserError('401 unauthorized')):
            self.Product._cron_magento_sync_stock()
        self.assertTrue(self.product.magento_stock_dirty, "must stay queued")
        log = self.last_log('stock')
        self.assertEqual(log.state, 'error')
        self.assertIn('401', log.error_message)

    def test_sync_now_pushes_the_three_channels(self):
        with self.patch_client(write_source_items={}, write_base_prices=[],
                               write_product_tax_class=[]) as mocks:
            self.product.magento_sync_now()
        mocks['write_source_items'].assert_called_once()
        mocks['write_base_prices'].assert_called_once()
        mocks['write_product_tax_class'].assert_called_once()

    # ── the stock matrix widget ────────────────────────────────
    def test_the_matrix_returns_rows_and_warehouse_columns(self):
        data = self.Product.magento_stock_matrix()
        self.assertTrue(any(w['code'] == self.warehouse.code
                            for w in data['warehouses']))
        self.assertTrue(any(row['sku'] == 'SKU-PHONE' for row in data['rows']))
        self.assertGreaterEqual(data['total'], 1)

    def test_the_matrix_can_be_searched_and_paged(self):
        self.assertTrue(self.Product.magento_stock_matrix(search='SKU-PHONE')['rows'])
        self.assertFalse(self.Product.magento_stock_matrix(search='nothing-matches')['rows'])
        self.assertLessEqual(len(self.Product.magento_stock_matrix(limit=1)['rows']), 1)


@tagged('post_install', '-at_install')
class TestMappingRefresh(MagentoCase):
    """The discovery calls that populate the mapping dropdowns."""

    def test_sources_are_upserted_by_code(self):
        Source = self.env['artaza.magento.source']
        with self.patch_client(fetch_sources=[
            {'source_code': 'default', 'name': 'Renamed', 'enabled': True},
            {'source_code': 'shop2', 'name': 'Shop 2', 'enabled': False},
            {'name': 'no code, skipped'},
        ]):
            created, updated = Source.refresh_from_magento()
        self.assertEqual((created, updated), (1, 1))
        self.assertEqual(self.source.name, 'Renamed')
        self.assertFalse(Source.search([('code', '=', 'shop2')]).enabled)

    def test_a_source_missing_from_magento_is_kept(self):
        """Deleting it would silently break a warehouse mapping."""
        with self.patch_client(fetch_sources=[]):
            self.env['artaza.magento.source'].refresh_from_magento()
        self.assertTrue(self.source.exists())

    def test_source_display_name(self):
        self.assertEqual(self.source.display_name, 'Default Source (default)')

    def test_tax_classes_are_refreshed_with_their_rate(self):
        TaxClass = self.env['artaza.magento.tax.class']
        with self.patch_client(
            fetch_product_tax_classes=[{'class_id': 4, 'class_name': 'Taxable Goods'},
                                       {'class_id': 7, 'class_name': 'Electronics'},
                                       {'class_name': 'no id, skipped'}],
            fetch_tax_rules=[{'tax_rate_ids': [1], 'product_tax_class_ids': [4]},
                             {'tax_rate_ids': [2, 3], 'product_tax_class_ids': [7]}],
            fetch_tax_rates=[{'id': 1, 'rate': 21.0}, {'id': 2, 'rate': 10.5},
                             {'id': 3, 'rate': 27.0}],
        ):
            created, updated = TaxClass.refresh_from_magento()
        self.assertEqual((created, updated), (1, 1))
        self.assertTrue(self.tax_class.single_rate)
        self.assertEqual(self.tax_class.rate, 21.0)
        # two rates on one class → not auto-matchable
        electronics = TaxClass.search([('magento_class_id', '=', 7)])
        self.assertFalse(electronics.single_rate)
        self.assertIn('no single rate', electronics.display_name)

    def test_tax_class_display_name_shows_the_rate(self):
        self.assertEqual(self.tax_class.display_name, 'Taxable Goods — 21%')

    # ── auto-match ─────────────────────────────────────────────
    def test_auto_match_pairs_a_tax_with_the_class_of_the_same_rate(self):
        self.tax.magento_tax_class_id = False
        with self.patch_client(
            fetch_tax_rules=[{'tax_rate_ids': [1], 'product_tax_class_ids': [4]}],
            fetch_tax_rates=[{'id': 1, 'rate': 21.0}],
        ):
            matched = self.tax.action_magento_auto_match()
        self.assertEqual(matched, 1)
        self.assertEqual(self.tax.magento_tax_class_id, self.tax_class)

    def test_auto_match_never_overwrites_a_human_choice(self):
        with self.patch_client(
            fetch_tax_rules=[{'tax_rate_ids': [1], 'product_tax_class_ids': [4]}],
            fetch_tax_rates=[{'id': 1, 'rate': 99.0}],
        ):
            self.assertEqual(self.tax.action_magento_auto_match(), 0)
        self.assertEqual(self.tax.magento_tax_class_id, self.tax_class)

    def test_auto_match_leaves_an_ambiguous_rate_pending(self):
        self.tax.magento_tax_class_id = False
        self.env['artaza.magento.tax.class'].create({
            'magento_class_id': 9, 'name': 'Other 21', 'rate': 21.0, 'single_rate': True,
        })
        with self.patch_client(
            fetch_tax_rules=[{'tax_rate_ids': [1], 'product_tax_class_ids': [4]},
                             {'tax_rate_ids': [1], 'product_tax_class_ids': [9]}],
            fetch_tax_rates=[{'id': 1, 'rate': 21.0}],
        ):
            self.assertEqual(self.tax.action_magento_auto_match(), 0)
        self.assertFalse(self.tax.magento_tax_class_id)

    def test_a_fixed_amount_tax_has_no_rate_to_match_on(self):
        fixed = self.env['account.tax'].create({
            'name': 'Fixed fee', 'amount': 5.0, 'amount_type': 'fixed',
            'type_tax_use': 'sale', 'country_id': self.tax.country_id.id,
        })
        self.assertIsNone(fixed._magento_rate())
        self.assertEqual(self.tax._magento_rate(), 21.0)

    # ── the silent-money guard ─────────────────────────────────
    def test_a_mapped_tax_that_is_not_price_included_is_flagged(self):
        gross = self.env['account.tax'].create({
            'name': 'VAT 10.5 not included', 'amount': 10.5, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'country_id': self.tax.country_id.id,
            'magento_tax_class_id': self.tax_class.id,
        })
        warnings = (self.tax + gross)._magento_price_include_warnings()
        self.assertEqual(warnings, gross)
        self.assertTrue(self.tax.magento_price_include_ok)
        self.assertFalse(gross.magento_price_include_ok)
