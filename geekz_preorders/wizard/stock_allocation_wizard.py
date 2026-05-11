from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PreorderStockAllocationWizard(models.TransientModel):
    """
    Wizard para asignación manual de stock a reservas activas.
    Útil cuando el admin quiere forzar la asignación sin esperar
    la llegada física de un picking.
    """
    _name = 'preorder.stock.allocation.wizard'
    _description = 'Asignación Manual de Stock a Preventas'

    product_tmpl_id = fields.Many2one(
        'product.template',
        string='Producto',
        required=True,
        domain=[('preorder_enabled', '=', True)],
    )
    product_id = fields.Many2one(
        'product.product',
        string='Variante',
        required=True,
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Bodega',
        required=True,
        default=lambda self: self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1
        ),
    )

    # Computed: info
    available_qty = fields.Float(
        string='Stock disponible',
        compute='_compute_available_qty',
        readonly=True,
    )
    pending_reservations = fields.Integer(
        string='Reservas activas',
        compute='_compute_pending_reservations',
        readonly=True,
    )
    pending_qty_total = fields.Integer(
        string='Unidades en espera',
        compute='_compute_pending_reservations',
        readonly=True,
    )

    @api.onchange('product_tmpl_id')
    def _onchange_product_tmpl(self):
        if self.product_tmpl_id:
            variants = self.product_tmpl_id.product_variant_ids
            self.product_id = variants[:1]
        else:
            self.product_id = False

    @api.depends('product_id', 'warehouse_id')
    def _compute_available_qty(self):
        for wiz in self:
            if wiz.product_id and wiz.warehouse_id:
                wiz.available_qty = wiz.product_id.with_context(
                    location=wiz.warehouse_id.lot_stock_id.id
                ).qty_available
            else:
                wiz.available_qty = 0.0

    @api.depends('product_id')
    def _compute_pending_reservations(self):
        Res = self.env['preorder.reservation']
        for wiz in self:
            if wiz.product_id:
                pending = Res.search([
                    ('product_id', '=', wiz.product_id.id),
                    ('status', '=', 'active'),
                ])
                wiz.pending_reservations = len(pending)
                wiz.pending_qty_total = sum(pending.mapped('quantity'))
            else:
                wiz.pending_reservations = 0
                wiz.pending_qty_total = 0

    def action_run_allocation(self):
        """Ejecutar asignación FIFO para el producto seleccionado."""
        self.ensure_one()
        if not self.product_id:
            raise UserError(_('Selecciona un producto.'))

        if self.available_qty <= 0:
            raise UserError(_(
                'No hay stock disponible en bodega para %s.',
                self.product_id.name,
            ))

        if self.pending_reservations == 0:
            raise UserError(_(
                'No hay reservas activas para %s.',
                self.product_id.name,
            ))

        self.env['preorder.reservation']._allocate_stock_for_product(
            self.product_id.id
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Asignación completada'),
                'message': _(
                    'Stock asignado a reservas FIFO para %s. '
                    'Clientes notificados por email.',
                    self.product_id.name,
                ),
                'type': 'success',
                'sticky': False,
            },
        }
