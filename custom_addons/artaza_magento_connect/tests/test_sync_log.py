"""The sync history — the module's whole reason for existing.

Two behaviours are load-bearing and get explicit tests: a failure must be
recorded even though the transaction that produced it rolls back, and a failed
*import* must be replayable, because its cursor has already moved past it.
"""
from unittest.mock import MagicMock, patch

from lxml import etree

from odoo import fields
from odoo.tests.common import tagged

from ..models.artaza_magento_sync_log import MAX_BODY_CHARS, MAX_REFS_CHARS, _clip
from .common import MagentoCase


@tagged('post_install', '-at_install')
class TestSyncLog(MagentoCase):

    def setUp(self):
        super().setUp()
        self.log = self.env['artaza.magento.sync.log']

    # ── helpers ────────────────────────────────────────────────
    def test_clip(self):
        self.assertFalse(_clip('', 10))
        self.assertFalse(_clip(None, 10))
        self.assertEqual(_clip('short', 10), 'short')
        self.assertTrue(_clip('x' * 50, 10).endswith('[…]'))
        self.assertEqual(_clip(123, 10), '123')  # non-str goes through repr

    # ── track / _store ─────────────────────────────────────────
    def test_success_run_is_recorded_as_one_line(self):
        with self.log.track('price', 'out', 'cron') as tracker:
            tracker.ok(['A1', 'A2'])
        rec = self.last_log('price')
        self.assertEqual(rec.state, 'success')
        self.assertEqual(rec.count_ok, 2)
        self.assertEqual(rec.count_error, 0)
        self.assertEqual(rec.ok_refs, 'A1, A2')
        # payloads are noise on a good run
        self.assertFalse(rec.request_payload)

    def test_partial_run_keeps_a_line_per_failure(self):
        with self.log.track('stock') as tracker:
            tracker.ok('A1')
            tracker.fail('A2', 'no source mapped')
        rec = self.last_log('stock')
        self.assertEqual(rec.state, 'partial')
        self.assertEqual(rec.count_ok, 1)
        self.assertEqual(len(rec.line_ids), 1)
        self.assertEqual(rec.line_ids.ref, 'A2')

    def test_a_raising_block_is_recorded_and_re_raised(self):
        """The exception must still reach the caller: the history observes, it
        does not swallow.

        Asserted on what reaches the writer rather than on the stored row: the
        row goes out on a cursor of its own (see below), which a test
        transaction cannot observe.
        """
        with self.capture_store() as stored:
            with self.assertRaises(ValueError):
                with self.log.track('tax') as tracker:
                    tracker.attempt(['A1', 'A2'])
                    raise ValueError('boom')
        self.assertEqual(stored['operation'], 'tax')
        self.assertIn('boom', stored['error_message'])
        # the refs it was about to send are kept, so a total failure can still
        # say WHAT did not go through and be re-queued
        self.assertEqual(sorted(stored['attempted']), ['A1', 'A2'])
        self.assertFalse(stored['ok_refs'])

    def test_the_history_row_is_written_on_a_cursor_of_its_own(self):
        """The bug the whole model fixes: a row written inside the failing
        transaction would roll back with it, taking the only trace with it."""
        real_cursor = self.registry.cursor
        used = []

        def spy(*args, **kwargs):
            used.append(1)
            return real_cursor(*args, **kwargs)

        with patch.object(self.registry, 'cursor', spy):
            with self.assertRaises(ValueError):
                with self.log.track('coupon') as tracker:
                    tracker.attempt('RMA-1')
                    raise ValueError('boom')
        self.assertTrue(used, "the history must not write on the failing cursor")

    def test_an_empty_run_is_not_recorded(self):
        before = self.log.search_count([])
        with self.log.track('price'):
            pass
        self.assertEqual(self.log.search_count([]), before)

    def test_long_refs_and_bodies_are_clipped(self):
        with self.log.track('stock') as tracker:
            tracker.ok(['SKU-%s' % i for i in range(2000)])
            tracker.fail('X', 'y' * (MAX_BODY_CHARS + 500))
        rec = self.last_log('stock')
        self.assertLessEqual(len(rec.ok_refs), MAX_REFS_CHARS + 5)
        self.assertLessEqual(len(rec.line_ids.error_message), MAX_BODY_CHARS + 5)

    def test_display_name_summarises_the_run(self):
        with self.log.track('price') as tracker:
            tracker.ok('A1')
        rec = self.last_log('price')
        self.assertIn('Price', rec.display_name)
        self.assertIn('1', rec.display_name)

    def test_tracker_ignores_empty_refs(self):
        with self.log.track('price') as tracker:
            tracker.ok(['A1', None, ''])
            tracker.attempt('A1')
        rec = self.last_log('price')
        self.assertEqual(rec.count_ok, 1)

    # ── retry: pushes ──────────────────────────────────────────
    def test_retry_push_requeues_the_products(self):
        self.product.magento_price_dirty = False
        rec = self.log.create({
            'operation': 'price', 'direction': 'out', 'state': 'error',
            'line_ids': [(0, 0, {'ref': self.product.default_code,
                                 'error_message': '401'})],
        })
        result = rec.action_retry()
        self.assertEqual(result['params']['type'], 'success')
        self.assertTrue(self.product.magento_price_dirty)

    def test_retry_push_warns_when_the_skus_are_gone(self):
        rec = self.log.create({
            'operation': 'stock', 'direction': 'out', 'state': 'error',
            'line_ids': [(0, 0, {'ref': 'SKU-VANISHED', 'error_message': 'x'})],
        })
        self.assertEqual(rec.action_retry()['params']['type'], 'warning')

    def test_retry_is_refused_for_operations_that_cannot_be_replayed(self):
        rec = self.log.create({
            'operation': 'config', 'direction': 'in', 'state': 'error',
            'line_ids': [(0, 0, {'ref': 'x', 'error_message': 'x'})],
        })
        self.assertEqual(rec.action_retry()['params']['type'], 'warning')

    # ── retry: pulls ───────────────────────────────────────────
    def test_retry_pull_reimports_and_marks_the_row_handled(self):
        rec = self.log.create({
            'operation': 'order_pull', 'direction': 'in', 'state': 'partial',
            'line_ids': [(0, 0, {'ref': '000000055', 'error_message': 'missing SKU'})],
        })
        with patch.object(type(self.env['sale.order']), '_magento_import_one',
                          MagicMock(return_value={'status': 'imported'})) as imp:
            result = rec.action_retry()
        imp.assert_called_once_with('000000055')
        self.assertEqual(result['params']['type'], 'success')
        self.assertTrue(rec.resolved)

    def test_retry_pull_keeps_the_row_pending_when_it_fails_again(self):
        rec = self.log.create({
            'operation': 'order_pull', 'direction': 'in', 'state': 'error',
            'line_ids': [(0, 0, {'ref': '000000055', 'error_message': 'x'})],
        })
        with patch.object(type(self.env['sale.order']), '_magento_import_one',
                          MagicMock(return_value={'status': 'error', 'message': 'still broken'})):
            result = rec.action_retry()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertTrue(result['params']['sticky'])
        self.assertIn('still broken', result['params']['message'])
        self.assertFalse(rec.resolved)

    def test_retry_pull_treats_an_already_present_record_as_done(self):
        rec = self.log.create({
            'operation': 'rma_pull', 'direction': 'in', 'state': 'error',
            'line_ids': [(0, 0, {'ref': 'RMA-1', 'error_message': 'x'})],
        })
        with patch.object(type(self.env['artaza.magento.rma']), '_magento_import_one',
                          MagicMock(return_value={'status': 'exists'})):
            self.assertEqual(rec.action_retry()['params']['type'], 'success')

    def test_retry_pull_without_refs_warns(self):
        rec = self.log.create({
            'operation': 'order_pull', 'direction': 'in', 'state': 'error',
        })
        self.assertEqual(rec.action_retry()['params']['type'], 'warning')

    def test_failed_refs_falls_back_to_the_ok_list(self):
        rec = self.log.create({
            'operation': 'price', 'direction': 'out', 'state': 'error',
            'ok_refs': 'A1, A2',
        })
        self.assertEqual(rec._failed_refs(), ['A1', 'A2'])

    # ── housekeeping ───────────────────────────────────────────
    def test_mark_resolved(self):
        rec = self.log.create({'operation': 'price', 'direction': 'out', 'state': 'error'})
        rec.action_mark_resolved()
        self.assertTrue(rec.resolved)

    def test_purge_deletes_old_successes_but_never_errors(self):
        old = fields.Datetime.subtract(fields.Datetime.now(), days=90)
        keep_error = self.log.create({'operation': 'price', 'direction': 'out',
                                      'state': 'error'})
        drop_ok = self.log.create({'operation': 'price', 'direction': 'out',
                                   'state': 'success'})
        fresh_ok = self.log.create({'operation': 'price', 'direction': 'out',
                                    'state': 'success'})
        self.env.cr.execute(
            "UPDATE artaza_magento_sync_log SET create_date = %s WHERE id IN %s",
            (old, (keep_error.id, drop_ok.id)),
        )
        self.log.invalidate_model(['create_date'])
        self.log._cron_purge_sync_logs()
        self.assertTrue(keep_error.exists())
        self.assertFalse(drop_ok.exists())
        self.assertTrue(fresh_ok.exists())

    def test_purge_is_a_no_op_when_retention_is_zero(self):
        self.icp.set_param('artaza_magento_connect.log_retention_days', '0')
        rec = self.log.create({'operation': 'price', 'direction': 'out', 'state': 'success'})
        self.env.cr.execute(
            "UPDATE artaza_magento_sync_log SET create_date = %s WHERE id = %s",
            (fields.Datetime.subtract(fields.Datetime.now(), days=900), rec.id),
        )
        self.assertEqual(self.log._cron_purge_sync_logs(), 0)
        self.assertTrue(rec.exists())

    # ── the failure line ───────────────────────────────────────
    def test_open_record_returns_an_action_when_linked(self):
        rec = self.log.create({
            'operation': 'order_pull', 'direction': 'in', 'state': 'error',
            'line_ids': [(0, 0, {'ref': 'x', 'error_message': 'x',
                                 'res_model': 'res.partner',
                                 'res_id': self.partner.id})],
        })
        action = rec.line_ids.action_open_record()
        self.assertEqual(action['res_model'], 'res.partner')
        self.assertEqual(action['res_id'], self.partner.id)

    def test_open_record_is_a_no_op_without_a_link(self):
        rec = self.log.create({
            'operation': 'price', 'direction': 'out', 'state': 'error',
            'line_ids': [(0, 0, {'ref': 'x', 'error_message': 'x'})],
        })
        self.assertFalse(rec.line_ids.action_open_record())

    def test_fail_can_link_back_to_a_record(self):
        with self.log.track('order_pull', 'in') as tracker:
            tracker.fail('x', 'boom', record=self.partner)
        rec = self.last_log('order_pull')
        self.assertEqual(rec.line_ids.res_model, 'res.partner')
        self.assertEqual(rec.line_ids.res_id, self.partner.id)

    # ── the form view ──────────────────────────────────────────
    def test_the_header_error_is_marked_red_on_the_form(self):
        """The run's own error sits outside any group, so it gets no label and
        renders as a bare line — red is the only thing that says it is the
        failure and not more metadata.

        This asserts the class reaches the stored arch, which is all a suite
        with no browser can see: it does not prove anything is painted, and a
        misspelt Bootstrap class would pass here and render black.
        """
        form = self.env.ref('artaza_magento_connect.view_magento_sync_log_form')
        arch = etree.fromstring(form.arch)
        header, = arch.xpath("//sheet/field[@name='error_message']")
        self.assertIn('text-danger', header.get('class', ''))
        # the per-item errors in the Failures tab keep the list's own styling
        line, = arch.xpath("//field[@name='line_ids']//field[@name='error_message']")
        self.assertNotIn('text-danger', line.get('class', ''))
