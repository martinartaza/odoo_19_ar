"""The settings screen: cron scheduling, the pull cursors and the buttons.

The cursor is the one setting that can silently lose data, so it gets the most
attention: emptying it must mean *from the beginning*, never *from now*.
"""
from odoo.exceptions import UserError
from odoo.tests.common import tagged

from ..models.artaza_magento_rma import CURSOR_PARAM as RMAS_CURSOR
from ..models.sale_order import CURSOR_PARAM as ORDERS_CURSOR
from ..models.sale_order import DEFAULT_CURSOR
from .common import MagentoCase


@tagged('post_install', '-at_install')
class TestConfig(MagentoCase):

    def setUp(self):
        super().setUp()
        self.Settings = self.env['res.config.settings']

    def new_settings(self):
        return self.Settings.create({})

    # ── cursors ────────────────────────────────────────────────
    def test_the_cursor_round_trips_through_the_screen(self):
        self.icp.set_param(ORDERS_CURSOR, '2026-03-01 08:00:00')
        settings = self.new_settings()
        self.assertEqual(str(settings.magento_orders_cursor), '2026-03-01 08:00:00')

        settings.magento_orders_cursor = '2026-05-04 09:30:00'
        settings.set_values()
        self.assertEqual(self.icp.get_param(ORDERS_CURSOR), '2026-05-04 09:30:00')

    def test_emptying_the_cursor_means_from_the_beginning(self):
        """Silently jumping to 'now' would skip the store's whole history."""
        settings = self.new_settings()
        settings.magento_orders_cursor = False
        settings.magento_rmas_cursor = False
        settings.set_values()
        self.assertEqual(self.icp.get_param(ORDERS_CURSOR), DEFAULT_CURSOR)
        self.assertEqual(self.icp.get_param(RMAS_CURSOR), DEFAULT_CURSOR)

    def test_an_unset_cursor_reads_as_the_default(self):
        self.icp.set_param(ORDERS_CURSOR, '')
        self.assertEqual(str(self.new_settings().magento_orders_cursor), DEFAULT_CURSOR)

    def test_the_returns_cursor_is_independent(self):
        settings = self.new_settings()
        settings.magento_rmas_cursor = '2026-02-02 02:02:02'
        settings.set_values()
        self.assertEqual(self.icp.get_param(RMAS_CURSOR), '2026-02-02 02:02:02')
        self.assertNotEqual(self.icp.get_param(ORDERS_CURSOR), '2026-02-02 02:02:02')

    # ── crons ──────────────────────────────────────────────────
    def test_the_screen_drives_the_crons(self):
        settings = self.new_settings()
        settings.magento_cron_active = True
        settings.magento_cron_interval_number = 45
        settings.magento_cron_interval_type = 'minutes'
        settings.magento_orders_cron_active = True
        settings.magento_orders_interval_number = 5
        settings.magento_rmas_cron_active = False
        settings.set_values()

        stock_cron = settings._magento_stock_cron()
        self.assertTrue(stock_cron.active)
        self.assertEqual(stock_cron.interval_number, 45)
        self.assertTrue(settings._magento_orders_cron().active)
        self.assertFalse(settings._magento_rmas_cron().active)

        reread = self.new_settings()
        self.assertTrue(reread.magento_cron_active)
        self.assertEqual(reread.magento_orders_interval_number, 5)

    def test_a_zero_frequency_is_clamped(self):
        settings = self.new_settings()
        settings.magento_cron_active = True
        settings.magento_cron_interval_number = 0
        settings.set_values()
        self.assertEqual(settings._magento_stock_cron().interval_number, 1)

    # ── mapping tabs ───────────────────────────────────────────
    def test_the_mapping_tabs_list_the_real_records(self):
        settings = self.new_settings()
        self.assertIn(self.warehouse, settings.magento_warehouse_ids)
        self.assertIn(self.tax, settings.magento_sale_tax_ids)

    def test_the_pending_problem_counter(self):
        self.env['artaza.magento.sync.log'].search([]).write({'resolved': True})
        before = self.new_settings().magento_log_error_count
        self.env['artaza.magento.sync.log'].create({
            'operation': 'price', 'direction': 'out', 'state': 'error',
        })
        self.assertEqual(self.new_settings().magento_log_error_count, before + 1)

    # ── buttons ────────────────────────────────────────────────
    def test_test_connection_reports_the_store_views(self):
        settings = self.new_settings()
        settings.magento_base_url = 'https://shop.example.com'
        settings.magento_token = 'tok-123'
        with self.patch_client(test_connection=[{'code': 'default'}, {'code': 'shop2'}]):
            result = settings.action_magento_test_direct_connection()
        self.assertIn('default', result['params']['message'])

    def test_the_connection_is_persisted_before_a_button_uses_it(self):
        """Without this, typing the URL and pressing a button fails with
        'not configured', because settings only save on Save."""
        settings = self.new_settings()
        settings.magento_base_url = 'https://typed-just-now.example.com'
        settings.magento_token = 'fresh-token'
        settings.magento_skip_ssl_verify = True
        settings._magento_persist_connection()
        cfg = self.env['artaza.magento.client']._get_config()
        self.assertEqual(cfg['base_url'], 'https://typed-just-now.example.com')
        self.assertFalse(cfg['verify'])

    def test_refresh_buttons_report_what_they_brought(self):
        settings = self.new_settings()
        with self.patch_client(fetch_sources=[{'source_code': 'new-one', 'name': 'New'}]):
            result = settings.action_magento_refresh_sources()
        self.assertEqual(result['params']['type'], 'success')

        with self.patch_client(fetch_product_tax_classes=[{'class_id': 11, 'class_name': 'X'}],
                               fetch_tax_rules=[], fetch_tax_rates=[]):
            result = settings.action_magento_refresh_tax_classes()
        self.assertEqual(result['params']['type'], 'success')

    def test_auto_match_button_shouts_about_a_gross_tax(self):
        """A mapped tax that is not price-included inflates every import."""
        self.env['account.tax'].create({
            'name': 'Gross 10.5', 'amount': 10.5, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'country_id': self.tax.country_id.id,
            'magento_tax_class_id': self.tax_class.id,
        })
        settings = self.new_settings()
        with self.patch_client(fetch_tax_rules=[], fetch_tax_rates=[]):
            result = settings.action_magento_automatch_taxes()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertTrue(result['params']['sticky'])

    def test_auto_match_button_lists_what_is_still_pending(self):
        self.env['account.tax'].create({
            'name': 'Unmapped 27', 'amount': 27.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'country_id': self.tax.country_id.id,
        })
        settings = self.new_settings()
        with self.patch_client(fetch_tax_rules=[], fetch_tax_rates=[]):
            result = settings.action_magento_automatch_taxes()
        self.assertEqual(result['params']['type'], 'warning')
        self.assertIn('Unmapped 27', result['params']['message'])

    def test_resync_buttons_queue_the_products(self):
        settings = self.new_settings()
        self.product.magento_stock_dirty = False
        self.product.magento_tax_dirty = False
        settings.action_magento_resync_all_stock()
        settings.action_magento_resync_all_taxes()
        self.assertTrue(self.product.magento_stock_dirty)
        self.assertTrue(self.product.magento_tax_dirty)

    def test_import_now_buttons_report_their_counts(self):
        settings = self.new_settings()
        with self.patch_client(fetch_orders=([],)):
            self.assertEqual(
                settings.action_magento_pull_orders()['params']['type'], 'success')
        with self.patch_client(fetch_rmas=([],)):
            self.assertEqual(
                settings.action_magento_pull_rmas()['params']['type'], 'success')

    def test_open_the_sync_history(self):
        action = self.new_settings().action_magento_open_sync_log()
        self.assertEqual(action['res_model'], 'artaza.magento.sync.log')

    def test_a_button_without_a_connection_fails_loudly(self):
        self.icp.set_param('artaza_magento_connect.magento_token', '')
        settings = self.new_settings()
        settings.magento_base_url = ''
        settings.magento_token = ''
        with self.assertRaises(UserError):
            settings.action_magento_test_direct_connection()
