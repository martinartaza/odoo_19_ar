"""The REST client. `requests` is mocked; no socket is ever opened.

What matters here is the shape of what leaves Odoo (URL, scope, auth header,
searchCriteria) and that no failure can escape as anything other than a
UserError the callers record in the history.
"""
from unittest.mock import MagicMock, patch

import requests

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from ..models.artaza_magento_client import (
    PARAM_MAGENTO_SKIP_SSL,
    PARAM_MAGENTO_TOKEN,
    PARAM_MAGENTO_URL,
    param_is_true,
)
from .common import MagentoCase


def _response(json_body=None, status=200, content=b'{}'):
    resp = MagicMock()
    resp.status_code = status
    resp.content = content
    resp.json.return_value = json_body if json_body is not None else {}
    resp.raise_for_status.return_value = None
    return resp


@tagged('post_install', '-at_install')
class TestClient(MagentoCase):

    def setUp(self):
        super().setUp()
        self.client = self.env['artaza.magento.client']

    # ── configuration ──────────────────────────────────────────
    def test_param_is_true(self):
        for truthy in ('True', 'true', '1', 'yes', ' TRUE '):
            self.assertTrue(param_is_true(truthy))
        for falsy in ('False', 'false', '0', '', None, 'no'):
            self.assertFalse(param_is_true(falsy))

    def test_config_reads_url_token_and_ssl_flag(self):
        self.icp.set_param(PARAM_MAGENTO_URL, 'https://shop.example.com/')
        cfg = self.client._get_config()
        self.assertEqual(cfg['base_url'], 'https://shop.example.com')  # trailing / stripped
        self.assertEqual(cfg['token'], 'tok-123')
        self.assertTrue(cfg['verify'])

    def test_config_verify_off_when_flag_set(self):
        self.icp.set_param(PARAM_MAGENTO_SKIP_SSL, 'True')
        self.assertFalse(self.client._get_config()['verify'])

    def test_config_missing_raises_a_readable_error(self):
        self.icp.set_param(PARAM_MAGENTO_TOKEN, '')
        with self.assertRaises(UserError):
            self.client._get_config()

    # ── call ───────────────────────────────────────────────────
    def test_call_builds_url_scope_and_auth(self):
        with patch.object(requests, 'request', return_value=_response({'ok': 1})) as req:
            body = self.client.call('GET', 'inventory/sources')
        self.assertEqual(body, {'ok': 1})
        args, kwargs = req.call_args
        self.assertEqual(args[0], 'GET')
        self.assertEqual(args[1], 'https://shop.example.com/rest/all/V1/inventory/sources')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer tok-123')
        self.assertTrue(kwargs['verify'])

    def test_call_uses_the_store_scope_when_given(self):
        with patch.object(requests, 'request', return_value=_response()) as req:
            self.client.call('GET', 'categories', store='default')
        self.assertIn('/rest/default/V1/categories', req.call_args[0][1])

    def test_call_returns_empty_dict_on_empty_body(self):
        with patch.object(requests, 'request', return_value=_response(content=b'')):
            self.assertEqual(self.client.call('POST', 'x'), {})

    def test_call_returns_empty_dict_on_non_json_body(self):
        resp = _response(content=b'not json')
        resp.json.side_effect = ValueError
        with patch.object(requests, 'request', return_value=resp):
            self.assertEqual(self.client.call('GET', 'x'), {})

    def test_http_error_becomes_a_user_error_carrying_magento_message(self):
        err_resp = MagicMock(status_code=401)
        err_resp.json.return_value = {'message': "The consumer isn't authorized."}
        resp = _response()
        resp.raise_for_status.side_effect = requests.HTTPError(response=err_resp)
        with patch.object(requests, 'request', return_value=resp):
            with self.assertRaises(UserError) as ctx:
                self.client.call('POST', 'x')
        self.assertIn('401', str(ctx.exception))

    def test_connection_error_becomes_a_user_error(self):
        with patch.object(requests, 'request',
                          side_effect=requests.ConnectionError('refused')):
            with self.assertRaises(UserError):
                self.client.call('GET', 'x')

    # ── search / pagination ────────────────────────────────────
    def test_search_builds_search_criteria(self):
        with patch.object(self.client.__class__, 'call',
                          MagicMock(return_value={'items': [], 'total_count': 0})) as call:
            self.client.search('orders',
                               filters=[('updated_at', '2026-01-01', 'gteq'),
                                        ('state', 'new', 'in')],
                               sort=('updated_at', 'ASC'), page_size=50)
        params = call.call_args[1]['params']
        self.assertEqual(params['searchCriteria[pageSize]'], 50)
        self.assertEqual(
            params['searchCriteria[filter_groups][0][filters][0][field]'], 'updated_at')
        self.assertEqual(
            params['searchCriteria[filter_groups][0][filters][0][condition_type]'], 'gteq')
        self.assertEqual(
            params['searchCriteria[filter_groups][1][filters][0][value]'], 'new')
        self.assertEqual(params['searchCriteria[sortOrders][0][direction]'], 'ASC')

    def test_search_follows_pages_until_total_count(self):
        pages = [{'items': [{'id': 1}, {'id': 2}], 'total_count': 3},
                 {'items': [{'id': 3}], 'total_count': 3}]
        with patch.object(self.client.__class__, 'call',
                          MagicMock(side_effect=pages)) as call:
            items = self.client.search('orders', page_size=2)
        self.assertEqual(len(items), 3)
        self.assertEqual(call.call_count, 2)

    def test_search_single_page_stops_after_one_call(self):
        with patch.object(self.client.__class__, 'call',
                          MagicMock(return_value={'items': [{'id': 1}], 'total_count': 99})) as call:
            items = self.client.search('orders', page_size=1, single_page=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(call.call_count, 1)

    def test_search_stops_on_short_page_when_total_is_absent(self):
        with patch.object(self.client.__class__, 'call',
                          MagicMock(return_value={'items': [{'id': 1}]})) as call:
            self.client.search('orders', page_size=10)
        self.assertEqual(call.call_count, 1)

    # ── the typed wrappers ─────────────────────────────────────
    def test_wrappers_hit_the_expected_endpoints(self):
        with patch.object(self.client.__class__, 'call', MagicMock(return_value={})) as call:
            self.client.test_connection()
            self.assertEqual(call.call_args[0][1], 'store/storeConfigs')

            self.client.write_source_items([{'sku': 'A'}])
            self.assertEqual(call.call_args[0][1], 'inventory/source-items')

            self.client.write_base_prices([{'sku': 'A', 'price': 1}])
            self.assertEqual(call.call_args[0][1], 'products/base-prices')

            self.client.get_order(7)
            self.assertEqual(call.call_args[0][1], 'orders/7')

            self.client.write_order_negotiation(7, 10.0, 'why', 110.0)
            self.assertEqual(call.call_args[0][1], 'orders/7/negotiation')
            self.assertEqual(call.call_args[1]['payload']['adjustmentAmount'], 10.0)

            self.client.write_rma_status(42, 'accepted', admin_message='ok')
            self.assertEqual(call.call_args[0][1], 'rma/42/status')
            self.assertEqual(call.call_args[1]['payload']['adminMessage'], 'ok')

    def test_order_actions_coerce_the_bare_integer_magento_returns(self):
        with patch.object(self.client.__class__, 'call', MagicMock(return_value='15')):
            self.assertEqual(self.client.create_invoice(7), 15)
        with patch.object(self.client.__class__, 'call', MagicMock(return_value=None)):
            self.assertEqual(self.client.create_shipment(7), 0)

    def test_fetch_by_increment_returns_one_or_none(self):
        with patch.object(self.client.__class__, 'search',
                          MagicMock(return_value=[{'increment_id': 'x'}])):
            self.assertEqual(self.client.fetch_order_by_increment('x')['increment_id'], 'x')
        with patch.object(self.client.__class__, 'search', MagicMock(return_value=[])):
            self.assertIsNone(self.client.fetch_order_by_increment('x'))
            self.assertIsNone(self.client.fetch_rma_by_increment('x'))

    def test_empty_write_payloads_do_not_call_magento(self):
        with patch.object(self.client.__class__, 'call', MagicMock()) as call:
            self.assertEqual(self.client.write_base_prices([]), [])
            self.assertEqual(self.client.write_source_items([]), {})
            self.assertEqual(self.client.write_product_tax_class(4, []), [])
        call.assert_not_called()

    def test_write_product_tax_class_reports_unknown_skus(self):
        with patch.object(self.client.__class__, 'call', MagicMock(return_value=['A', 'B'])):
            self.assertEqual(self.client.write_product_tax_class(4, ['A', 'B', 'C']), ['A', 'B'])
        with patch.object(self.client.__class__, 'call', MagicMock(return_value={})):
            self.assertEqual(self.client.write_product_tax_class(4, ['A']), [])

    # ── error extraction ───────────────────────────────────────
    def test_extract_error_interpolates_magento_parameters(self):
        resp = MagicMock()
        resp.json.return_value = {'message': '%fieldName is required',
                                  'parameters': {'fieldName': 'sku'}}
        self.assertEqual(self.client._extract_error(resp), 'sku is required')

    def test_extract_error_interpolates_positional_parameters(self):
        resp = MagicMock()
        resp.json.return_value = {'message': 'No such entity %1', 'parameters': ['sku']}
        self.assertEqual(self.client._extract_error(resp), 'No such entity sku')

    def test_extract_error_falls_back_to_text_and_body(self):
        self.assertEqual(self.client._extract_error(None), 'no response')

        resp = MagicMock(text='<html>boom</html>')
        resp.json.side_effect = ValueError
        self.assertEqual(self.client._extract_error(resp), '<html>boom</html>')

        resp2 = MagicMock()
        resp2.json.return_value = {'errors': [1]}
        self.assertIn('errors', self.client._extract_error(resp2))

    def test_fetch_rmas_filters_by_status_when_asked(self):
        with patch.object(self.client.__class__, 'search', MagicMock(return_value=[])) as search:
            self.client.fetch_rmas('2026-01-01', 50, statuses='requested')
        self.assertEqual(len(search.call_args[1]['filters']), 2)
