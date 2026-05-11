from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # ── Campos de preventa ────────────────────────────────────────────────────

    preorder_enabled = fields.Boolean(
        string='Habilitado para Preventa',
        default=False,
        help='Reemplaza el botón "Agregar al carrito" con "Reservar Preventa" en la tienda.',
    )
    preorder_eta = fields.Date(
        string='Fecha Estimada de Llegada',
        help='Fecha aproximada en que el producto llegará a bodega.',
    )
    preorder_message = fields.Text(
        string='Mensaje de Preventa',
        help='Texto que se muestra al cliente en la página del producto.',
        default='Este producto está disponible en preventa. '
                'Te notificaremos por email cuando llegue a bodega.',
    )
    preorder_max_qty_per_customer = fields.Integer(
        string='Máx. cantidad por cliente',
        default=1,
        help='Máximo de unidades que un mismo cliente puede reservar de este producto.',
    )
    preorder_visible_stock = fields.Boolean(
        string='Mostrar stock reservado',
        default=False,
        help='Muestra al cliente cuántas unidades están reservadas actualmente.',
    )
    preorder_limit_total = fields.Integer(
        string='Límite total de reservas',
        default=0,
        help='0 = sin límite. Si > 0, se cierra la preventa al llegar a ese número.',
    )

    # ── Computed: estadísticas de reservas ────────────────────────────────────

    preorder_active_count = fields.Integer(
        string='Reservas activas',
        compute='_compute_preorder_stats',
        help='Total de reservas activas + notificadas para este producto.',
    )
    preorder_total_qty_reserved = fields.Integer(
        string='Unidades reservadas',
        compute='_compute_preorder_stats',
    )

    def _compute_preorder_stats(self):
        Reservation = self.env['preorder.reservation']
        for tmpl in self:
            reservations = Reservation.search([
                ('product_tmpl_id', '=', tmpl.id),
                ('status', 'in', ('active', 'allocated', 'notified')),
            ])
            tmpl.preorder_active_count = len(reservations)
            tmpl.preorder_total_qty_reserved = sum(reservations.mapped('quantity'))

    # ── Validaciones ──────────────────────────────────────────────────────────

    @api.constrains('preorder_max_qty_per_customer')
    def _check_preorder_max_qty(self):
        for tmpl in self:
            if tmpl.preorder_max_qty_per_customer < 1:
                raise ValidationError(
                    _('La cantidad máxima por cliente debe ser al menos 1.')
                )

    # ── Acciones ──────────────────────────────────────────────────────────────

    def action_view_preorder_reservations(self):
        """Open reservations for this product template from the product form."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'preorder.reservation',
            'view_mode': 'list,form,kanban',
            'domain': [('product_tmpl_id', '=', self.id)],
            'context': {'default_product_id': self.product_variant_ids[:1].id},
            'name': _('Reservas de %s') % self.name,
        }


class ProductProduct(models.Model):
    _inherit = 'product.product'

    # Related field para acceso directo en templates QWeb del website
    preorder_enabled = fields.Boolean(
        related='product_tmpl_id.preorder_enabled',
        store=True,
        readonly=True,
    )
    preorder_eta = fields.Date(
        related='product_tmpl_id.preorder_eta',
        readonly=True,
    )
    preorder_message = fields.Text(
        related='product_tmpl_id.preorder_message',
        readonly=True,
    )
    preorder_max_qty_per_customer = fields.Integer(
        related='product_tmpl_id.preorder_max_qty_per_customer',
        readonly=True,
    )
