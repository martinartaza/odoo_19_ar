"""Synchronisation history — every failure visible on a screen, zero commands.

This is Objective 2 of ``integration_v4.md`` (§4). It exists because on
2026-08-06 a tax-class rollout failed for three days in a row with a perfectly
clear message (``403: Missing scope: tax:write``) that only ever reached
``docker compose logs``: the cron caught the exception, wrote a
``_logger.warning`` and moved on. Meanwhile the price push — which runs first in
the same cron — kept succeeding and made the integration look healthy.

Two rules shape the model:

- **Errors are detailed and never auto-deleted**; successes collapse to a single
  row listing the refs that went through, and are purged after N days. Nobody
  audits a success item by item; every error needs to say exactly what failed
  and let you jump to the record.
- **The log is written on its own database cursor.** If the push raises, the
  main transaction rolls back — and a log record written inside it would roll
  back with it, which is precisely the failure mode this module is fixing.
  In Magento terms: the audit row goes out on a separate connection so the
  rollback of the main transaction cannot take it down.
"""
import logging
import time
from contextlib import contextmanager

from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)

# Keep rows small: these columns are for a human reading a screen, not a dump.
MAX_REFS_CHARS = 4000
MAX_BODY_CHARS = 8000

OPERATIONS = [
    ('stock', "Stock"),
    ('price', "Price"),
    ('tax', "Tax class"),
    ('order_pull', "Order import"),
    ('rma_pull', "Return import"),
    ('rma_status', "Return status"),
    ('negotiation', "Negotiation"),
    ('shipment', "Shipment"),
    ('coupon', "Coupon"),
    ('config', "Configuration"),
]

# How a failed run is replayed. Pushes are re-queued through the product flag the
# cron reads; pulls are re-fetched one record at a time by their Magento number,
# because the cursor has already moved past them (see `action_retry`).
PUSH_RETRY_FLAGS = {
    'stock': 'magento_stock_dirty',
    'price': 'magento_price_dirty',
    'tax': 'magento_tax_dirty',
}
PULL_RETRY = {
    'order_pull': ('sale.order', '_magento_import_one'),
    'rma_pull': ('artaza.magento.rma', '_magento_import_one'),
}


def _clip(value, limit):
    if not value:
        return False
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + ' […]'


class MagentoSyncTracker:
    """Collects what happened during one operation.

    A plain object on purpose: it lives inside the failing transaction, so it
    must not touch the ORM — everything it gathers is handed to the log writer
    afterwards, on a clean cursor.
    """

    def __init__(self):
        self.ok_refs = []
        self.attempted = []
        self.errors = []
        self.request_payload = None
        self.response_body = None
        self.http_status = 0
        self.note = None

    def attempt(self, refs):
        """Refs about to be sent — kept so a total failure can still list them.

        Without this, a batch that blows up before any per-item result exists
        would leave no SKUs to re-queue, which is the one thing you want after
        a failed run.
        """
        if isinstance(refs, str):
            refs = [refs]
        self.attempted.extend(str(ref) for ref in refs if ref)

    def ok(self, refs):
        """Record refs (SKUs, order numbers…) that went through."""
        if isinstance(refs, str):
            refs = [refs]
        self.ok_refs.extend(str(ref) for ref in refs if ref)

    def fail(self, ref, message, record=None):
        """Record one item that did not make it, with a link back to it."""
        self.errors.append({
            'ref': str(ref or '-'),
            'error_message': _clip(str(message), MAX_BODY_CHARS),
            'res_model': record._name if record is not None and record else False,
            'res_id': record.id if record is not None and record else False,
        })


class MagentoSyncLog(models.Model):
    _name = 'artaza.magento.sync.log'
    _description = 'Magento sync log'
    _order = 'create_date desc, id desc'

    operation = fields.Selection(OPERATIONS, string="Operation", required=True, index=True)
    direction = fields.Selection(
        [('out', "Odoo → Magento"), ('in', "Magento → Odoo")],
        string="Direction", required=True, default='out',
    )
    state = fields.Selection(
        [('success', "Success"), ('partial', "Partial"), ('error', "Error")],
        string="Result", required=True, index=True,
    )
    trigger = fields.Selection(
        [('cron', "Cron"), ('manual', "Manual"), ('ui', "Screen")],
        string="Triggered by", default='cron',
    )
    started_at = fields.Datetime(string="Started at")
    duration = fields.Float(string="Duration (s)", digits=(10, 2))
    count_ok = fields.Integer(string="OK")
    count_error = fields.Integer(string="Failed")
    ok_refs = fields.Text(
        string="Sent OK",
        help="One line per operation, not per item: the refs that went through.",
    )
    http_status = fields.Integer(string="HTTP status")
    error_message = fields.Text(string="Error")
    request_payload = fields.Text(string="Request sent")
    response_body = fields.Text(string="Response")
    note = fields.Char(string="Note")
    line_ids = fields.One2many(
        'artaza.magento.sync.log.line', 'log_id', string="Failures",
    )
    resolved = fields.Boolean(
        string="Handled", default=False,
        help="Tick to take an error off the pending list without deleting it. "
             "Errors are never purged automatically.",
    )

    @api.depends('operation', 'state', 'count_ok', 'count_error')
    def _compute_display_name(self):
        labels = dict(OPERATIONS)
        for log in self:
            log.display_name = '%s · %s' % (
                labels.get(log.operation, log.operation or ''),
                self.env._("%s failed", log.count_error) if log.count_error
                else self.env._("%s OK", log.count_ok),
            )

    # ── The instrumentation entry point ────────────────────────
    @contextmanager
    def track(self, operation, direction='out', trigger='cron', note=None):
        """Wrap one operation so its outcome always lands in the history.

        Usage::

            with self.env['artaza.magento.sync.log'].track('price') as tracker:
                tracker.ok([p.default_code for p in products])
                push(products)

        On an exception the failure is recorded and the exception is re-raised:
        deciding whether to stop or carry on stays with the caller, but staying
        silent is no longer an option.
        """
        tracker = MagentoSyncTracker()
        tracker.note = note
        started = fields.Datetime.now()
        clock = time.time()
        try:
            yield tracker
        except Exception as exc:
            self._store(
                operation, direction, trigger, tracker, started, clock,
                error_message=str(exc),
            )
            raise
        self._store(operation, direction, trigger, tracker, started, clock)

    def _store(self, operation, direction, trigger, tracker, started, clock,
               error_message=None):
        """Write the outcome on a NEW cursor so a rollback cannot erase it."""
        if error_message:
            state = 'error'
            # The whole batch died, so nothing in it actually went through: the
            # refs collected up front were *attempted*, not sent. Turning each
            # one into a failure line keeps the counters honest and gives
            # `action_retry` the exact list of SKUs to re-queue.
            refs = tracker.attempted or tracker.ok_refs
            if not tracker.errors and refs:
                tracker.errors = [{
                    'ref': ref,
                    'error_message': _clip(error_message, MAX_BODY_CHARS),
                    'res_model': False,
                    'res_id': False,
                } for ref in refs]
            tracker.ok_refs = []
        elif tracker.errors:
            state = 'partial'
        elif not tracker.ok_refs and not tracker.attempted:
            # Nothing to do is not worth a row; it would bury the real ones.
            return
        else:
            state = 'success'

        values = {
            'operation': operation,
            'direction': direction,
            'trigger': trigger,
            'state': state,
            'started_at': started,
            'duration': round(time.time() - clock, 2),
            'count_ok': len(tracker.ok_refs),
            'count_error': len(tracker.errors),
            'ok_refs': _clip(', '.join(tracker.ok_refs), MAX_REFS_CHARS),
            'http_status': tracker.http_status or 0,
            'error_message': _clip(error_message, MAX_BODY_CHARS),
            'note': tracker.note,
            'line_ids': [(0, 0, line) for line in tracker.errors],
        }
        # Payloads are kept only when something went wrong: on a good run they
        # are noise, and they are the bulk of the row's weight.
        if state != 'success':
            values['request_payload'] = _clip(tracker.request_payload, MAX_BODY_CHARS)
            values['response_body'] = _clip(tracker.response_body, MAX_BODY_CHARS)

        try:
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env['artaza.magento.sync.log'].create(values)
        except Exception:  # noqa: BLE001 - the log must never break the sync
            _logger.exception("Could not write the Magento sync log (%s)", operation)

    # ── Actions ────────────────────────────────────────────────
    def action_mark_resolved(self):
        self.write({'resolved': True})

    def action_retry(self):
        """Put the failed items back in flight.

        Two mechanisms, because the two directions fail differently. A **push**
        is driven by the `dirty` flags on the product, so re-queueing is enough
        — the next cron tick sends it again. A **pull** cannot wait for the next
        tick: the cursor has already moved past the record, so nothing would
        ever fetch it again. Those are re-imported here and now, one by one, by
        their Magento number — the same path as the "import one by number"
        wizard, which is precisely what bypasses the cursor.
        """
        self.ensure_one()
        if self.operation in PULL_RETRY:
            return self._retry_pull()
        return self._retry_push()

    def _failed_refs(self):
        """The refs to act on: the failure lines, or whatever was attempted."""
        refs = [line.ref for line in self.line_ids if line.ref and line.ref != '-']
        if not refs and self.ok_refs:
            refs = [ref.strip() for ref in self.ok_refs.split(',') if ref.strip()]
        return refs

    def _retry_pull(self):
        """Re-import each failed record by its Magento number, cursor ignored."""
        model, method = PULL_RETRY[self.operation]
        refs = self._failed_refs()
        if not refs:
            return self._notify('warning', self.env._(
                "This run recorded no record number to re-import.",
            ))

        done, failed = [], []
        for ref in refs:
            result = getattr(self.env[model], method)(ref)
            if result.get('status') in ('imported', 'exists'):
                done.append(ref)
            else:
                failed.append('%s: %s' % (
                    ref, result.get('message') or result.get('status'),
                ))

        if failed:
            # Still broken: leave the row pending so it stays on the operator's
            # list, and say exactly what Magento or Odoo answered this time.
            return self._notify('danger', self.env._(
                "%(done)s re-imported, %(failed)s still failing — %(detail)s",
                done=len(done), failed=len(failed), detail=" | ".join(failed),
            ), sticky=True)

        # Everything came in: the row has served its purpose, take it off the
        # pending list instead of making the operator tick it by hand.
        self.resolved = True
        return self._notify('success', self.env._(
            "%s record(s) re-imported: %s", len(done), ", ".join(done),
        ))

    def _retry_push(self):
        """Flag the products again so the next cron tick re-sends them."""
        flag = PUSH_RETRY_FLAGS.get(self.operation)
        if not flag:
            return self._notify('warning', self.env._(
                "This kind of run cannot be replayed automatically.",
            ))
        skus = self._failed_refs()
        products = self.env['product.product'].search([('default_code', 'in', skus)])
        if not products:
            return self._notify('warning', self.env._(
                "None of those SKUs exist in Odoo any more.",
            ))
        products.write({flag: True})
        return self._notify('success', self.env._(
            "%s product(s) re-queued. The cron will send them again.", len(products),
        ))

    def _notify(self, kind, message, sticky=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': kind,
                'title': self.env._("Sync history"),
                'message': message,
                # A failure list is read, not glanced at: it must not self-close.
                'sticky': sticky,
            },
        }

    # ── Retention ──────────────────────────────────────────────
    @api.model
    def _cron_purge_sync_logs(self):
        """Delete OLD SUCCESSFUL runs. Errors are never purged automatically.

        An old error is exactly the one that explains today's odd behaviour,
        and errors are few by definition — if they are many, the table is not
        the problem.
        """
        icp = self.env['ir.config_parameter'].sudo()
        days = int(icp.get_param('artaza_magento_connect.log_retention_days') or 30)
        if days <= 0:
            return 0
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.search([
            ('state', '=', 'success'),
            ('create_date', '<', limit),
        ])
        count = len(stale)
        stale.unlink()
        if count:
            _logger.info("Magento sync log: purged %s successful run(s).", count)
        return count


class MagentoSyncLogLine(models.Model):
    _name = 'artaza.magento.sync.log.line'
    _description = 'Magento sync failure'
    _order = 'id'

    log_id = fields.Many2one(
        'artaza.magento.sync.log', string="Run", required=True, ondelete='cascade', index=True,
    )
    ref = fields.Char(
        string="Reference",
        help="SKU / order number. Kept as text so it survives the record being deleted.",
    )
    error_message = fields.Text(string="Reason")
    res_model = fields.Char(string="Model")
    res_id = fields.Integer(string="Record id")
    operation = fields.Selection(
        related='log_id.operation', string="Operation", store=True,
    )
    state = fields.Selection(related='log_id.state', string="Result", store=True)

    def action_open_record(self):
        """Jump to the product / order / return that failed."""
        self.ensure_one()
        if not (self.res_model and self.res_id):
            return False
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': self.res_id,
            'view_mode': 'form',
            'target': 'current',
        }
