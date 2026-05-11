{
    'name': 'GeekZ Preventas',
    'version': '19.0.1.0.0',
    'summary': 'Sistema de reservas de preventa sin pago inicial para GeekZ',
    'description': """
        Módulo integral de preventas para GeekZ (Chile).
        - Reservas sin pago inicial
        - Cola FIFO con asignación automática de stock
        - Notificaciones por email
        - Penalización por incumplimiento
        - Integración con website_sale, stock e inventario
    """,
    'author': 'GeekZ SPA',
    'website': 'https://www.geekz.cl',
    'category': 'Sales/eCommerce',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'portal',
        'stock',
        'sale_management',
        'website_sale',
    ],
    'data': [
        # Security primero
        'security/preorder_security.xml',
        'security/ir.model.access.csv',
        # Data base
        'data/sequences.xml',
        'data/mail_templates.xml',
        'data/cron_jobs.xml',
        # Vistas backend
        'views/preorder_dashboard_views.xml',
        'views/preorder_reservation_views.xml',
        'views/product_template_views.xml',
        'views/res_partner_views.xml',
        'views/menus.xml',
        # Vistas website
        'views/website_preorder_templates.xml',
        # Wizards
        'wizard/stock_allocation_wizard_views.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
