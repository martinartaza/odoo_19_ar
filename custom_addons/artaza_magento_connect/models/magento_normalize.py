"""Turn Magento's raw REST payloads into the flat shape Odoo absorbs.

Kept as **module-level pure functions**: no ORM, no `self`, so the trickiest
part of the integration — reading Magento's large, nested payloads — can be
reasoned about and tested on its own.
"""


def _address(raw):
    """Magento address → flat dict. `street` is a list in Magento, a string here."""
    raw = raw or {}
    street = raw.get('street')
    if isinstance(street, list):
        street = ', '.join(part for part in street if part)
    return {
        'firstname': raw.get('firstname'),
        'lastname': raw.get('lastname'),
        'street': street,
        'city': raw.get('city'),
        'region': raw.get('region'),
        'postcode': raw.get('postcode'),
        'country_id': raw.get('country_id'),
        'telephone': raw.get('telephone'),
        'vat_id': raw.get('vat_id'),
    }


def _shipping_address(raw):
    """The shipping address hides under extension_attributes.shipping_assignments."""
    assignments = (raw.get('extension_attributes') or {}).get('shipping_assignments') or []
    if assignments:
        return (assignments[0].get('shipping') or {}).get('address') or {}
    return {}


def extract_items(raw_items):
    """Flatten order lines to (simple SKU, tax-included unit price, qty).

    A configurable purchase arrives as **two** lines: the configurable parent
    carries the price and quantity, the simple child carries the SKU Odoo
    actually stocks. Neither line alone is usable, so they are combined —
    child SKU + parent price/qty.

    The price sent is **tax included**: Odoo runs "tax included in price" and
    back-calculates the net, same convention as the price sync.
    """
    children_by_parent = {}
    for item in raw_items or []:
        parent_id = item.get('parent_item_id')
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(item)

    items = []
    for item in raw_items or []:
        if item.get('parent_item_id'):
            continue  # absorbed through its parent
        if item.get('product_type') == 'configurable':
            child = (children_by_parent.get(item.get('item_id')) or [None])[0]
            sku = (child or item).get('sku')
            name = (child or item).get('name')
        else:
            sku = item.get('sku')
            name = item.get('name')
        if not sku:
            continue
        items.append({
            'sku': sku,
            'name': name,
            'qty': item.get('qty_ordered') or 0,
            'price': item.get('price_incl_tax') or item.get('price') or 0,
        })
    return items


def normalize_order(raw):
    """Magento order → the flat dict `_magento_absorb_order` expects."""
    billing = raw.get('billing_address') or {}
    payment = raw.get('payment') or {}
    extension = raw.get('extension_attributes') or {}
    return {
        'increment_id': raw.get('increment_id'),
        'entity_id': raw.get('entity_id'),
        'state': raw.get('state'),
        'status': raw.get('status'),
        'created_at': raw.get('created_at'),
        'updated_at': raw.get('updated_at'),
        'grand_total': raw.get('grand_total'),
        # Tax-included freight, with the net amount as a fallback.
        'shipping_amount': raw.get('shipping_incl_tax') or raw.get('shipping_amount'),
        'currency': raw.get('order_currency_code'),
        'payment_method': payment.get('method'),
        'customer': {
            'email': raw.get('customer_email'),
            'firstname': raw.get('customer_firstname') or billing.get('firstname'),
            'lastname': raw.get('customer_lastname') or billing.get('lastname'),
            'is_guest': bool(raw.get('customer_is_guest')),
            # AR fiscal condition, added to the order by Artaza_CheckoutHyvaTheme.
            'afip_responsibility': extension.get('afip_responsibility'),
        },
        'billing': _address(billing),
        'shipping': _address(_shipping_address(raw)),
        'items': extract_items(raw.get('items') or []),
    }


def normalize_rma(raw):
    """Magento RMA → the flat dict `_magento_absorb_rma` expects.

    Thin on purpose: the only real translation is `entity_id` → `rma_id`, since
    Magento's RMA payload was designed for this trip.
    """
    items = []
    for item in raw.get('items') or []:
        if not item.get('sku'):
            continue
        items.append({
            'sku': item.get('sku'),
            'name': item.get('name'),
            'qty_requested': item.get('qty_requested') or 0,
            'order_item_id': item.get('order_item_id'),
        })
    return {
        'rma_id': raw.get('entity_id'),
        'increment_id': raw.get('increment_id'),
        'order_id': raw.get('order_id'),
        'order_increment_id': raw.get('order_increment_id'),
        'store_id': raw.get('store_id'),
        'customer_id': raw.get('customer_id'),
        'customer_email': raw.get('customer_email'),
        'type': raw.get('type'),
        'status': raw.get('status'),
        'reason_code': raw.get('reason_code'),
        'customer_note': raw.get('customer_note'),
        'resolution': raw.get('resolution'),
        'admin_message': raw.get('admin_message'),
        'credit_amount': raw.get('credit_amount'),
        'coupon_code': raw.get('coupon_code'),
        'odoo_reference': raw.get('odoo_reference'),
        'created_at': raw.get('created_at'),
        'updated_at': raw.get('updated_at'),
        'items': items,
    }


def is_fully_invoiced(order):
    grand_total = float(order.get('grand_total') or 0)
    invoiced = float(order.get('total_invoiced') or 0)
    return grand_total > 0 and invoiced >= grand_total


def is_fully_shipped(order):
    """True when every physical line is fully shipped.

    Skips configurable children (the parent line carries the quantity) and
    virtual items, which are not shippable. Returns False when there is nothing
    shippable at all, so callers treat it as a no-op rather than "done".
    """
    shippable = False
    for item in order.get('items') or []:
        if item.get('parent_item_id') or item.get('is_virtual'):
            continue
        ordered = float(item.get('qty_ordered') or 0)
        if ordered <= 0:
            continue
        shippable = True
        if float(item.get('qty_shipped') or 0) < ordered:
            return False
    return shippable
