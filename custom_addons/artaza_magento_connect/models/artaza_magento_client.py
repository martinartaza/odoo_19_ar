"""REST client to the Magento 2 API.

Every call Odoo makes to Magento goes through here: discovery (sources, tax
classes, store views), pushes (stock, price, tax class, negotiation, invoice,
shipment, RMA status, coupons) and cursor-based pulls (orders, returns).

Authentication is the access token of a Magento *integration*, sent as
``Authorization: Bearer <token>``. Errors surface as ``UserError`` with
Magento's own message, and the callers record them in the sync history — a
failed call must never end as a silent log line.
"""
import logging
import warnings

import requests
from urllib3.exceptions import InsecureRequestWarning

from odoo import api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ir.config_parameter keys (v4 — direct connection)
PARAM_MAGENTO_URL = 'artaza_magento_connect.magento_base_url'
PARAM_MAGENTO_TOKEN = 'artaza_magento_connect.magento_token'
# Expresses the NON-default state on purpose: `res.config.settings` deletes the
# parameter when a Boolean is unticked, so a "verify SSL" flag defaulting to
# True could never be turned off (it would always read back as the default).
PARAM_MAGENTO_SKIP_SSL = 'artaza_magento_connect.magento_skip_ssl_verify'

REQUEST_TIMEOUT = 30
PAGE_SIZE = 200


def param_is_true(value):
    """ir.config_parameter values are strings; '' / 'False' / None are false."""
    return str(value or '').strip().lower() in ('true', '1', 'yes')


class MagentoClient(models.AbstractModel):
    """HTTP client to the Magento REST API.

    Authenticates with the **integration access token** as
    ``Authorization: Bearer <token>``. It is an AbstractModel: used via
    ``self.env['artaza.magento.client']``.
    """
    _name = 'artaza.magento.client'
    _description = 'Magento REST client (direct)'

    # -- Configuration -------------------------------------------------------

    @api.model
    def _get_config(self):
        ICP = self.env['ir.config_parameter'].sudo()
        base_url = (ICP.get_param(PARAM_MAGENTO_URL) or '').strip().rstrip('/')
        token = (ICP.get_param(PARAM_MAGENTO_TOKEN) or '').strip()
        if not (base_url and token):
            raise UserError(self.env._(
                "The Magento connection is not configured. Set the Magento URL "
                "and the integration token in Settings ▸ Magento Connect.",
            ))
        # A local dev stack usually serves HTTPS with a self-signed certificate;
        # production must keep verification on, which is the default precisely
        # because the flag has to be opted into.
        return {
            'base_url': base_url,
            'token': token,
            'verify': not param_is_true(ICP.get_param(PARAM_MAGENTO_SKIP_SSL)),
        }

    # -- Generic call --------------------------------------------------------

    @api.model
    def call(self, method, path, payload=None, params=None, store='all'):
        """Run an authenticated call against Magento's REST API.

        :param method: 'GET' | 'POST' | 'PUT' | 'DELETE'
        :param path: path after the version, e.g. 'inventory/sources'
        :param payload: dict/list sent as JSON
        :param params: query string dict
        :param store: store code used in the path. 'all' = admin scope; a real
            store code is required for anything that is store-scoped (see the
            categories gotcha in integration_v3.md §v3.2.a)
        :return: the decoded JSON body
        """
        cfg = self._get_config()
        url = '%s/rest/%s/V1/%s' % (cfg['base_url'], store, path.lstrip('/'))
        headers = {
            'Authorization': 'Bearer %s' % cfg['token'],
            'Content-Type': 'application/json',
        }

        try:
            with warnings.catch_warnings():
                if not cfg['verify']:
                    # The operator opted out of verification on purpose (local
                    # dev with a self-signed certificate); one warning per call
                    # would drown the log without telling them anything new.
                    warnings.simplefilter('ignore', InsecureRequestWarning)
                resp = requests.request(
                    method, url, json=payload, params=params, headers=headers,
                    timeout=REQUEST_TIMEOUT, verify=cfg['verify'],
                )
            resp.raise_for_status()
        except requests.HTTPError as exc:
            detail = self._extract_error(exc.response)
            status = exc.response.status_code if exc.response is not None else '?'
            _logger.warning("Magento %s %s -> %s: %s", method, url, status, detail)
            raise UserError(self.env._(
                "Magento rejected the operation (%(code)s): %(detail)s",
                code=status, detail=detail,
            )) from exc
        except requests.RequestException as exc:
            raise UserError(self.env._(
                "Connection error with Magento: %s", exc,
            )) from exc

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    @api.model
    def search(self, path, filters=None, page_size=PAGE_SIZE, sort=None,
               single_page=False):
        """GET a `searchCriteria` endpoint, following pagination.

        `filters` is a list of `(field, value, condition_type)`; each one goes
        into its own filter group (AND between groups). `sort` is
        `(field, 'ASC'|'DESC')`.

        `single_page=True` returns just the first page: the cursor-based pulls
        want one page at a time so the cursor advances as each page is absorbed.
        """
        items = []
        page = 1
        while True:
            params = {
                'searchCriteria[pageSize]': page_size,
                'searchCriteria[currentPage]': page,
            }
            for index, (field, value, condition) in enumerate(filters or []):
                prefix = 'searchCriteria[filter_groups][%s][filters][0]' % index
                params['%s[field]' % prefix] = field
                params['%s[value]' % prefix] = value
                params['%s[condition_type]' % prefix] = condition
            if sort:
                params['searchCriteria[sortOrders][0][field]'] = sort[0]
                params['searchCriteria[sortOrders][0][direction]'] = sort[1]
            body = self.call('GET', path, params=params)
            batch = body.get('items') or []
            items.extend(batch)
            if single_page:
                break
            # `total_count` is authoritative; fall back to a short page.
            total = body.get('total_count')
            if total is None:
                if len(batch) < page_size:
                    break
            elif len(items) >= total or not batch:
                break
            page += 1
        return items

    # -- Discovery (feeds the mapping screens) -------------------------------

    @api.model
    def test_connection(self):
        """Cheap authenticated call used by the 'Test connection' button."""
        return self.call('GET', 'store/storeConfigs')

    @api.model
    def fetch_sources(self):
        """MSI inventory sources: [{source_code, name, enabled}]."""
        return self.search('inventory/sources')

    @api.model
    def fetch_product_tax_classes(self):
        """Product tax classes only — customer classes are not mappable."""
        return self.search(
            'taxClasses/search', filters=[('class_type', 'PRODUCT', 'eq')],
        )

    @api.model
    def fetch_tax_rules(self):
        """Tax rules: they link product tax classes to tax rates."""
        return self.search('taxRules/search')

    @api.model
    def fetch_tax_rates(self):
        """Tax rates: {id, rate} as a percentage."""
        return self.search('taxRates/search')

    # -- Writes ---------------------------------------------------------------

    @api.model
    def write_base_prices(self, prices):
        """Bulk-write base prices: [{sku, price, store_id}].

        Magento answers 200 with an **array of per-item errors** (empty = all
        good), so a partial failure looks like success at the HTTP level. That
        array is returned for the caller to record — exactly the kind of quiet
        half-failure the sync history exists to surface.
        """
        if not prices:
            return []
        body = self.call('POST', 'products/base-prices', payload={'prices': prices})
        return body if isinstance(body, list) else []

    @api.model
    def write_source_items(self, source_items):
        """Bulk-write MSI source items: [{sku, source_code, quantity, status}]."""
        if not source_items:
            return {}
        return self.call(
            'POST', 'inventory/source-items', payload={'sourceItems': source_items},
        )

    @api.model
    def write_product_tax_class(self, tax_class_id, skus):
        """Assign a product tax class to a batch of SKUs (Artaza endpoint).

        Returns the SKUs Magento did not find. Writes only `tax_class_id`, so
        nothing else about the product is touched.
        """
        if not skus:
            return []
        body = self.call(
            'POST', 'products/tax-class',
            payload={'taxClassId': tax_class_id, 'skus': skus},
        )
        return [str(sku) for sku in body] if isinstance(body, list) else []

    # -- Orders ---------------------------------------------------------------

    @api.model
    def fetch_orders(self, updated_since, states, page_size):
        """Orders updated since a cursor, filtered by state.

        Sorted **ASC by `updated_at`** on purpose: that is what makes the cursor
        self-healing — a run that dies half-way resumes from the last order it
        actually absorbed instead of skipping ahead.
        """
        return self.search(
            'orders',
            filters=[
                ('updated_at', updated_since, 'gteq'),
                ('state', states, 'in'),
            ],
            page_size=page_size,
            sort=('updated_at', 'ASC'),
            # One page per call: the caller advances its cursor as it absorbs.
            single_page=True,
        )

    @api.model
    def fetch_order_by_increment(self, increment_id):
        """One order by its number, any state (manual import, bypasses cursor)."""
        items = self.search(
            'orders', filters=[('increment_id', increment_id, 'eq')], page_size=1,
        )
        return items[0] if items else None

    @api.model
    def get_order(self, order_id):
        """The full order by entity id — used to check invoiced/shipped state."""
        return self.call('GET', 'orders/%s' % order_id)

    @api.model
    def write_order_negotiation(self, order_id, adjustment_amount,
                                adjustment_reason, negotiation_total):
        """Display-only negotiation fields on an order (Artaza endpoint).

        Magento expects camelCase keys on this endpoint, unlike the snake_case
        Odoo uses everywhere else.
        """
        return self.call('POST', 'orders/%s/negotiation' % order_id, payload={
            'adjustmentAmount': adjustment_amount,
            'adjustmentReason': adjustment_reason,
            'negotiationTotal': negotiation_total,
        })

    # -- Fulfillment ----------------------------------------------------------

    @api.model
    def create_invoice(self, order_id, notify=False):
        """Full invoice → order becomes `processing` (shippable).

        `capture=False`: offline payment, no online capture. Magento invoices the
        ORIGINAL grand total — the fiscal document with the negotiated total is
        the Odoo invoice, never this one.
        """
        return self._order_action(order_id, 'invoice', {
            'capture': False, 'notify': notify, 'appendComment': False,
        })

    @api.model
    def create_shipment(self, order_id, notify=True):
        """Full shipment → order becomes `complete` and the reservation is freed."""
        return self._order_action(order_id, 'ship', {'notify': notify})

    @api.model
    def _order_action(self, order_id, action, body):
        """POST order/{id}/{invoice|ship}; Magento answers a bare integer id."""
        result = self.call('POST', 'order/%s/%s' % (order_id, action), payload=body)
        try:
            return int(result)
        except (TypeError, ValueError):
            return 0

    # -- Returns (Artaza_Rma) -------------------------------------------------

    @api.model
    def fetch_rmas(self, updated_since, page_size, statuses=None):
        """RMAs updated since a cursor. ASC by `updated_at`, like orders."""
        filters = [('updated_at', updated_since, 'gteq')]
        if statuses:
            filters.append(('status', statuses, 'in'))
        return self.search(
            'rma', filters=filters, page_size=page_size,
            sort=('updated_at', 'ASC'), single_page=True,
        )

    @api.model
    def fetch_rma_by_increment(self, increment_id):
        """One RMA by its number, any status (replay of a failed import)."""
        items = self.search(
            'rma', filters=[('increment_id', increment_id, 'eq')], page_size=1,
        )
        return items[0] if items else None

    @api.model
    def write_rma_status(self, rma_id, status, admin_message=None, resolution=None,
                         credit_amount=None, coupon_code=None, odoo_reference=None):
        """Push the operator's decision onto a Magento RMA (camelCase body)."""
        return self.call('POST', 'rma/%s/status' % rma_id, payload={
            'status': status,
            'adminMessage': admin_message,
            'resolution': resolution,
            'creditAmount': credit_amount,
            'couponCode': coupon_code,
            'odooReference': odoo_reference,
        })

    # -- Coupons (RMA store credit) -------------------------------------------

    @api.model
    def fetch_store_views(self):
        """Store views: id, code, website_id, name."""
        return self.call('GET', 'store/storeViews') or []

    @api.model
    def fetch_customer_group_ids(self):
        """Every customer group id, plus 0 (NOT LOGGED IN).

        The coupon must work whether the recipient redeems it logged in or as a
        guest, and Magento's search does not always return group 0.
        """
        items = self.search('customerGroups/search')
        ids = [int(group['id']) for group in items if group.get('id') is not None]
        if 0 not in ids:
            ids.append(0)
        return sorted(set(ids))

    @api.model
    def create_cart_price_rule(self, rule):
        """POST /V1/salesRules; returns the new rule_id."""
        body = self.call('POST', 'salesRules', payload={'rule': rule})
        return int(body['rule_id'])

    @api.model
    def create_specific_coupon(self, rule_id, code):
        """POST /V1/coupons; attaches a specific code to a rule."""
        body = self.call('POST', 'coupons', payload={
            'coupon': {'rule_id': rule_id, 'code': code, 'is_primary': True},
        })
        return int(body['coupon_id'])

    @staticmethod
    def _extract_error(response):
        """Best-effort readable message out of a Magento error body."""
        if response is None:
            return 'no response'
        try:
            body = response.json()
        except ValueError:
            return (response.text or '')[:500]
        message = body.get('message')
        if not message:
            return str(body)[:500]
        # Magento returns "%fieldName is required" + parameters to interpolate.
        parameters = body.get('parameters')
        if isinstance(parameters, dict):
            for key, value in parameters.items():
                message = message.replace('%%%s' % key, str(value))
        elif isinstance(parameters, list):
            for index, value in enumerate(parameters, start=1):
                message = message.replace('%%%s' % index, str(value))
        return message[:500]
