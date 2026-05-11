from odoo import _, api, fields, models

BLOCK_THRESHOLD = 3


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # ── Campos de seguimiento de preventas ───────────────────────────────────

    preorder_expired_count = fields.Integer(
        string='Reservas expiradas',
        default=0,
        readonly=True,
        help='Número acumulado de preventas que el cliente dejó expirar sin pagar.',
    )
    preorder_completed_count = fields.Integer(
        string='Reservas completadas',
        default=0,
        readonly=True,
        help='Número de preventas que el cliente completó exitosamente.',
    )
    preorder_blocked = fields.Boolean(
        string='Bloqueado para preventas',
        default=False,
        tracking=True,
        help='Si está activo, el cliente no puede crear nuevas reservas de preventa.',
    )
    preorder_score = fields.Float(
        string='Score de preventa',
        compute='_compute_preorder_score',
        store=True,
        help='Ratio de completación. 1.0 = 100% de reservas completadas.',
    )

    # ── Computed ──────────────────────────────────────────────────────────────

    @api.depends('preorder_completed_count', 'preorder_expired_count')
    def _compute_preorder_score(self):
        for partner in self:
            total = partner.preorder_completed_count + partner.preorder_expired_count
            if total == 0:
                partner.preorder_score = 1.0
            else:
                partner.preorder_score = partner.preorder_completed_count / total

    preorder_active_count = fields.Integer(
        string='Reservas activas',
        compute='_compute_preorder_active_count',
    )

    def _compute_preorder_active_count(self):
        Reservation = self.env['preorder.reservation']
        for partner in self:
            partner.preorder_active_count = Reservation.search_count([
                ('customer_id', '=', partner.id),
                ('status', 'in', ('active', 'allocated', 'notified')),
            ])

    # ── Métodos internos ──────────────────────────────────────────────────────

    def _preorder_increment_expired(self):
        self.ensure_one()
        self.preorder_expired_count += 1
        if self.preorder_expired_count >= BLOCK_THRESHOLD:
            self.preorder_blocked = True
            self.message_post(
                body=_(
                    'Cliente bloqueado automáticamente para preventas: '
                    'acumuló %d reservas expiradas sin pago.',
                    self.preorder_expired_count,
                )
            )

    def _preorder_increment_completed(self):
        self.ensure_one()
        self.preorder_completed_count += 1

    # ── Acciones ──────────────────────────────────────────────────────────────

    def action_preorder_unblock(self):
        self.ensure_one()
        self.preorder_blocked = False
        self.message_post(body=_('Cliente desbloqueado manualmente para preventas.'))

    def action_view_preorder_reservations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'preorder.reservation',
            'view_mode': 'list,form',
            'domain': [('customer_id', '=', self.id)],
            'name': _('Reservas de %s') % self.name,
        }
