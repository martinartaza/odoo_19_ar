{
    'name': 'Magento 2 Connector',
    'version': '19.0.1.0.0',
    'category': 'Sales',
    'summary': 'Direct Odoo to Magento 2 sync: stock, prices, tax class, orders, '
               'returns and fulfillment — no middleware',
    'description': """
Magento 2 Connector
===================

Odoo talks **straight** to Magento 2 over its REST API. There is no middleware to
deploy, no second database and no extra set of credentials: Odoo authenticates
with the access token of a Magento *integration*.

What it syncs
-------------

* **Stock** per warehouse to a Magento MSI *inventory source*, summing the
  warehouses that share one source.
* **Base price**, tax included (Magento running *Catalog Prices = Including Tax*).
* **Product tax class**, auto-matched by rate.
* **Orders** Magento to Odoo, on a self-healing cursor you can see and edit. Paid
  orders arrive as a confirmed sale, unpaid offline ones as a quotation.
* **Negotiated total**: type the figure you agreed on and the discount is prorated
  across the lines, landing exactly on it. Display-only in Magento.
* **Fulfillment**: validate the delivery in Odoo and Magento is invoiced and shipped.
* **Returns (RMA)**: state machine, restock decision, credit note and coupon.

How it is configured
--------------------

Everything lives in *Settings > Magento Connect*. The relations between the two
systems (warehouse to source, tax to tax class) are picked from **lists the
Magento API fills in** — there is not a single field where you type an id.

When something fails
--------------------

Every run is recorded in a **sync history** you read inside Odoo. Failures keep the
detail per item, are never purged automatically and can be retried from the screen;
successes collapse to one line and are purged by age.

Some features need a companion Magento module, both free and OSL-3.0 licensed:
``artaza/module-odoo-integration`` and ``artaza/module-rma``.
""",
    'author': 'Sebastian Martin Artaza Saade',
    'maintainer': 'Sebastian Martin Artaza Saade',
    'website': 'https://www.artaza.net',
    'support': 'martin.artaza@gmail.com',
    'license': 'LGPL-3',
    # Card image on the app listing. The full page is static/description/index.html.
    'images': ['static/description/banner.png'],
    'depends': ['base', 'account', 'stock', 'sale', 'sale_stock'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'wizard/artaza_magento_order_import_wizard_views.xml',
        'views/res_config_settings_views.xml',
        'views/artaza_magento_rma_views.xml',
        'views/artaza_magento_sync_log_views.xml',
        'views/menus.xml',
        'views/product_stock_views.xml',
        'views/sale_order_views.xml',
        'wizard/artaza_magento_order_total_wizard_views.xml',
        'wizard/artaza_magento_rma_approve_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'artaza_magento_connect/static/src/**/*.js',
            'artaza_magento_connect/static/src/**/*.scss',
            'artaza_magento_connect/static/src/**/*.xml',
        ],
    },
    'external_dependencies': {
        'python': ['requests'],
    },
    'application': True,
    'installable': True,
}
