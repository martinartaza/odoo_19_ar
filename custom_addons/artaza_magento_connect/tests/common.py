"""Shared fixtures for the Magento connector tests.

The Magento side is always mocked: a test must never depend on a reachable
store. `patch_client` swaps methods on the client AbstractModel, which is the
single door every outbound call goes through — patching there keeps the tests
honest about *what* the module sends without ever opening a socket.
"""
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from ..models.artaza_magento_client import (
    PARAM_MAGENTO_SKIP_SSL,
    PARAM_MAGENTO_TOKEN,
    PARAM_MAGENTO_URL,
)


class MagentoCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The sync history writes on a cursor of its own so a rollback cannot
        # erase it. Registry test mode makes that cursor a TestCursor wrapping
        # the test's transaction, so the behaviour is exercised for real without
        # committing anything.
        cls.registry_enter_test_mode_cls()

        cls.icp = cls.env['ir.config_parameter'].sudo()
        cls.icp.set_param(PARAM_MAGENTO_URL, 'https://shop.example.com')
        cls.icp.set_param(PARAM_MAGENTO_TOKEN, 'tok-123')
        cls.icp.set_param(PARAM_MAGENTO_SKIP_SSL, False)

        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.company.id)], limit=1,
        )
        cls.source = cls.env['artaza.magento.source'].create({
            'code': 'default', 'name': 'Default Source',
        })
        cls.warehouse.magento_source_id = cls.source

        cls.tax_class = cls.env['artaza.magento.tax.class'].create({
            'magento_class_id': 4, 'name': 'Taxable Goods',
            'rate': 21.0, 'single_rate': True,
        })
        cls.tax = cls.env['account.tax'].create({
            'name': 'VAT 21% (test)',
            'amount': 21.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'price_include_override': 'tax_included',
            'country_id': cls.company.account_fiscal_country_id.id
                          or cls.env.ref('base.us').id,
            'magento_tax_class_id': cls.tax_class.id,
        })

        cls.product = cls.env['product.product'].create({
            'name': 'Test phone',
            'default_code': 'SKU-PHONE',
            'is_storable': True,
            'list_price': 1210.0,
            'taxes_id': [(6, 0, cls.tax.ids)],
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Test buyer', 'email': 'buyer@example.com',
        })

    def setUp(self):
        super().setUp()
        # History rows are written on a cursor of their own, so they do not
        # follow the test's savepoints. Watermark the table instead of trusting
        # isolation: assertions then read only what THIS test produced.
        self._log_mark = self.env['artaza.magento.sync.log'].search(
            [], limit=1, order='id desc').id or 0

    @contextmanager
    def capture_store(self):
        """Capture what the history writer was handed, without storing it.

        A failure is recorded on a cursor of its own so a rollback cannot erase
        it. That is the right design and the reason a test transaction cannot
        read the row back — `TestCursor.close()` rolls its savepoint back. So
        the failure paths assert on what reached the writer instead.
        """
        captured = {}
        log_cls = type(self.env['artaza.magento.sync.log'])
        real_store = log_cls._store

        def spy(records, operation, direction, trigger, tracker, started, clock,
                error_message=None):
            captured.update(
                operation=operation, direction=direction, trigger=trigger,
                error_message=error_message or '',
                errors=list(tracker.errors), ok_refs=list(tracker.ok_refs),
                attempted=list(tracker.attempted),
            )
            return real_store(records, operation, direction, trigger, tracker,
                              started, clock, error_message=error_message)

        with patch.object(log_cls, '_store', spy):
            yield captured

    def last_log(self, operation=None):
        """The most recent history row written by the running test."""
        domain = [('id', '>', self._log_mark)]
        if operation:
            domain.append(('operation', '=', operation))
        return self.env['artaza.magento.sync.log'].search(
            domain, limit=1, order='id desc')

    # ── helpers ────────────────────────────────────────────────
    @contextmanager
    def patch_client(self, **methods):
        """Replace client methods with MagicMocks for the block's duration.

        A MagicMock is not a descriptor, so it does not receive `self` — the
        assertions read exactly the arguments the module meant to send.

        The value decides the behaviour:

        * an **exception** instance → the call raises it;
        * a **tuple** → successive calls return its items in order (that is how
          a paginated pull is simulated: one page, then an empty one);
        * anything else → every call returns it.
        """
        client_cls = type(self.env['artaza.magento.client'])
        patches, mocks = [], {}
        for name, result in methods.items():
            if isinstance(result, BaseException) or (
                isinstance(result, type) and issubclass(result, BaseException)
            ):
                mock = MagicMock(side_effect=result)
            elif isinstance(result, tuple):
                mock = MagicMock(side_effect=list(result))
            else:
                mock = MagicMock(return_value=result)
            patches.append(patch.object(client_cls, name, mock))
            mocks[name] = mock
        for p in patches:
            p.start()
        try:
            yield mocks
        finally:
            for p in patches:
                p.stop()

    def make_order_payload(self, increment_id='000000001', **overrides):
        """A Magento order payload already in the normalized (absorb) shape."""
        payload = {
            'increment_id': increment_id,
            'entity_id': 900,
            'state': 'new',
            'status': 'pending',
            'updated_at': '2026-08-13 10:00:00',
            'grand_total': 1210.0,
            'shipping_amount': 0.0,
            'currency': 'USD',
            'payment_method': 'checkmo',
            'customer': {
                'email': 'buyer@example.com',
                'firstname': 'Test', 'lastname': 'Buyer',
                'is_guest': False, 'afip_responsibility': None,
            },
            'billing': {}, 'shipping': {},
            'items': [{
                'sku': 'SKU-PHONE', 'name': 'Test phone',
                'qty': 1.0, 'price': 1210.0,
            }],
        }
        payload.update(overrides)
        return payload

    def make_rma_payload(self, increment_id='RMA-0001', **overrides):
        payload = {
            'rma_id': 42,
            'increment_id': increment_id,
            'order_id': 900,
            'order_increment_id': '000000001',
            'customer_email': 'buyer@example.com',
            'type': 'exchange',
            'status': 'requested',
            'reason_code': 'damaged',
            'customer_note': 'It arrived broken',
            'updated_at': '2026-08-13 11:00:00',
            'items': [{
                'sku': 'SKU-PHONE', 'name': 'Test phone',
                'qty_requested': 1.0, 'order_item_id': 7,
            }],
        }
        payload.update(overrides)
        return payload
