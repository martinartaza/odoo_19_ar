{
    'name': 'Artaza Magento Connect',
    'version': '19.0.6.0.0',
    'category': 'Website/CMS',
    'summary': 'Gestiona el contenido CMS de Magento desde Odoo vía el middleware FastAPI',
    'description': """
Artaza Magento Connect
======================

Integración Odoo ⇄ FastAPI ⇄ Magento (Fase 1: CMS).

Permite crear y editar **CMS Pages** y **CMS Blocks** de Magento directamente
desde Odoo. Al guardar, el contenido se envía (push inmediato) al middleware
FastAPI mediante HTTPS, autenticando con una **API key** (``Authorization:
Bearer <key>``), y el middleware lo aplica en Magento a través de su REST API.

Ver el contrato compartido ``integration_v2.md`` para el detalle de payloads,
endpoints y autenticación.
    """,
    'author': 'Sebastian Martin Artaza Saade',
    'maintainer': 'Sebastian Martin Artaza Saade',
    'website': 'https://www.sebastianartaza.com',
    'support': 'martin.artaza@gmail.com',
    'license': 'LGPL-3',
    'depends': ['base', 'stock'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/cms_page_views.xml',
        'views/cms_block_views.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
        'views/product_stock_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'artaza_magento_connect/static/src/**/*.js',
            'artaza_magento_connect/static/src/**/*.xml',
        ],
    },
    'external_dependencies': {
        'python': ['requests'],
    },
    'application': True,
    'installable': True,
}
