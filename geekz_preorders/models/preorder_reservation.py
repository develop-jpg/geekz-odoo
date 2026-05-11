import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Transiciones válidas entre estados
_STATUS_TRANSITIONS = {
    'draft':     ['active', 'cancelled'],
    'active':    ['allocated', 'cancelled'],
    'allocated': ['notified', 'completed', 'cancelled'],
    'notified':  ['completed', 'expired', 'cancelled'],
    'completed': [],
    'expired':   [],
    'cancelled': [],
}

MAX_ACTIVE_RESERVATIONS_PER_CUSTOMER = 5
EXPIRATION_DAYS = 7
BLOCK_THRESHOLD_EXPIRED = 3


class PreorderReservation(models.Model):
    _name = 'preorder.reservation'
    _description = 'Reserva de Preventa GeekZ'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'reservation_date asc, id asc'
    _rec_name = 'name'

    # ── Identificación ────────────────────────────────────────────────────────

    name = fields.Char(
        string='Referencia',
        readonly=True,
        copy=False,
        default=lambda self: _('Nueva'),
    )
    company_id = fields.Many2one(
        'res.company',
        string='Empresa',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    # ── Relaciones principales ────────────────────────────────────────────────

    customer_id = fields.Many2one(
        'res.partner',
        string='Cliente',
        required=True,
        tracking=True,
        index=True,
        domain=[('customer_rank', '>', 0)],
        ondelete='restrict',
    )
    product_id = fields.Many2one(
        'product.product',
        string='Producto',
        required=True,
        tracking=True,
        index=True,
        ondelete='restrict',
    )
    product_tmpl_id = fields.Many2one(
        'product.template',
        related='product_id.product_tmpl_id',
        store=True,
        index=True,
        readonly=True,
    )
    warehouse_id = fields.Many2one(
        'stock.warehouse',
        string='Bodega',
        required=True,
        default=lambda self: self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1
        ),
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Orden de Venta',
        readonly=True,
        copy=False,
        tracking=True,
    )

    # ── Cantidades ────────────────────────────────────────────────────────────

    quantity = fields.Integer(
        string='Cantidad',
        default=1,
        required=True,
        tracking=True,
    )

    # ── Estado ────────────────────────────────────────────────────────────────

    status = fields.Selection(
        selection=[
            ('draft',     'Borrador'),
            ('active',    'En Cola de Espera'),
            ('allocated', 'Stock Reservado'),
            ('notified',  'Pendiente de Pago'),
            ('completed', 'Completada'),
            ('expired',   'Expirada'),
            ('cancelled', 'Cancelada'),
        ],
        string='Estado',
        default='draft',
        required=True,
        tracking=True,
        index=True,
    )

    # ── Fechas ────────────────────────────────────────────────────────────────

    reservation_date = fields.Datetime(
        string='Fecha de Reserva',
        default=fields.Datetime.now,
        readonly=True,
        copy=False,
        index=True,
        help='Determina la prioridad FIFO. No modificar manualmente.',
    )
    allocation_date = fields.Datetime(
        string='Fecha de Asignación',
        readonly=True,
        copy=False,
    )
    expiration_date = fields.Datetime(
        string='Fecha de Vencimiento',
        tracking=True,
        help='Plazo para pagar antes de liberar el stock asignado.',
    )
    completion_date = fields.Datetime(
        string='Fecha de Completación',
        readonly=True,
        copy=False,
    )

    # ── Campos calculados ─────────────────────────────────────────────────────

    queue_position = fields.Integer(
        string='Posición en Cola',
        compute='_compute_queue_position',
        help='Posición FIFO entre reservas activas del mismo producto.',
    )
    days_until_expiration = fields.Integer(
        string='Días hasta vencimiento',
        compute='_compute_days_until_expiration',
    )

    # ── Otros ────────────────────────────────────────────────────────────────

    customer_email = fields.Char(
        related='customer_id.email',
        string='Email cliente',
        readonly=True,
        store=False,
    )
    notes = fields.Text(string='Notas internas')
    active = fields.Boolean(default=True)
    reminder_sent = fields.Boolean(
        string='Recordatorio enviado',
        default=False,
        copy=False,
        help='Indica si se envió el recordatorio de 24h antes del vencimiento.',
    )

    # ── Constraints SQL ───────────────────────────────────────────────────────

    _positive_quantity = models.Constraint(
        'CHECK(quantity > 0)',
        'La cantidad debe ser mayor a cero.',
    )

    # ── ORM Overrides ─────────────────────────────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Nueva')) == _('Nueva'):
                vals['name'] = (
                    self.env['ir.sequence'].next_by_code('preorder.reservation')
                    or _('Nueva')
                )
        records = super().create(vals_list)
        # Auto-activar reservas creadas desde el backend (estado borrador)
        records.filtered(lambda r: r.status == 'draft').action_activate()
        return records

    # ── Computed ──────────────────────────────────────────────────────────────

    @api.depends('reservation_date', 'product_id', 'status')
    def _compute_queue_position(self):
        for rec in self:
            if rec.status != 'active' or not rec.product_id or not rec.reservation_date:
                rec.queue_position = 0
                continue
            pos = self.search_count([
                ('product_id', '=', rec.product_id.id),
                ('status', '=', 'active'),
                ('reservation_date', '<', rec.reservation_date),
                ('id', '!=', rec._origin.id),
            ])
            rec.queue_position = pos + 1

    @api.depends('expiration_date')
    def _compute_days_until_expiration(self):
        now = fields.Datetime.now()
        for rec in self:
            if rec.expiration_date and rec.expiration_date > now:
                rec.days_until_expiration = (rec.expiration_date - now).days
            else:
                rec.days_until_expiration = 0

    # ── Validaciones ──────────────────────────────────────────────────────────

    @api.constrains('quantity')
    def _check_quantity(self):
        for rec in self:
            if rec.quantity <= 0:
                raise ValidationError(_('La cantidad debe ser mayor a cero.'))

    def _validate_status_transition(self, new_status):
        """Enforce allowed state machine transitions."""
        status_labels = dict(self._fields['status'].selection)
        for rec in self:
            allowed = _STATUS_TRANSITIONS.get(rec.status, [])
            if new_status not in allowed:
                raise UserError(_(
                    'Transición inválida: %(desde)s → %(hacia)s.\n'
                    'Estados permitidos desde "%(desde)s": %(permitidos)s',
                    desde=status_labels.get(rec.status, rec.status),
                    hacia=status_labels.get(new_status, new_status),
                    permitidos=', '.join(status_labels.get(s, s) for s in allowed) or 'ninguno',
                ))

    def _check_customer_eligibility(self):
        """Validate partner is not blocked and within active reservation limit."""
        for rec in self:
            partner = rec.customer_id
            if partner.preorder_blocked:
                raise UserError(_(
                    'El cliente "%s" está bloqueado para realizar preventas '
                    'por acumular demasiadas reservas expiradas. '
                    'Contactar al administrador para desbloquear.',
                    partner.name,
                ))
            active_count = self.search_count([
                ('customer_id', '=', partner.id),
                ('status', 'in', ('active', 'allocated', 'notified')),
                ('id', '!=', rec.id),
            ])
            if active_count >= MAX_ACTIVE_RESERVATIONS_PER_CUSTOMER:
                raise UserError(_(
                    'El cliente "%s" ya tiene %d reservas activas (límite máximo permitido).',
                    partner.name,
                    MAX_ACTIVE_RESERVATIONS_PER_CUSTOMER,
                ))

    # ── Acciones de estado ────────────────────────────────────────────────────

    def action_activate(self):
        self._validate_status_transition('active')
        self._check_customer_eligibility()
        self.write({'status': 'active'})
        for rec in self:
            rec.message_post(body=_(
                '✅ Reserva confirmada. Cliente añadido a la lista de espera '
                '(posición #%d).', rec.queue_position
            ))
            rec._send_confirmation_email()

    def action_allocate(self):
        self._validate_status_transition('allocated')
        self.write({
            'status': 'allocated',
            'allocation_date': fields.Datetime.now(),
        })
        self.message_post(body=_('📦 Stock reservado en bodega para este cliente.'))

    def action_notify(self):
        self._validate_status_transition('notified')
        expiry = fields.Datetime.now() + timedelta(days=EXPIRATION_DAYS)
        for rec in self:
            rec.write({
                'status': 'notified',
                'expiration_date': expiry,
                'reminder_sent': False,
            })
            rec._send_allocation_email()
            rec.message_post(body=_(
                '📧 Cliente notificado por email. Tiene hasta el %s para '
                'completar el pago, de lo contrario el stock se libera al siguiente en cola.',
                expiry.strftime('%d/%m/%Y %H:%M')
            ))

    def action_expire(self):
        self._validate_status_transition('expired')
        self.write({'status': 'expired'})
        for rec in self:
            rec.customer_id.sudo()._preorder_increment_expired()
            rec.message_post(body=_(
                '⏰ Reserva expirada. El cliente no completó el pago dentro del plazo. '
                'El stock fue liberado para el siguiente cliente en cola.'
            ))
        self._trigger_reallocation()

    def action_cancel(self):
        self._validate_status_transition('cancelled')
        self.write({'status': 'cancelled'})
        self.message_post(body=_(
            '🚫 Reserva cancelada. El stock fue liberado para el siguiente cliente en cola.'
        ))
        self._trigger_reallocation()

    def action_complete(self):
        self._validate_status_transition('completed')
        self.write({
            'status': 'completed',
            'completion_date': fields.Datetime.now(),
        })
        for rec in self:
            rec.customer_id.sudo()._preorder_increment_completed()
            rec.message_post(body=_(
                '🎉 Preventa completada exitosamente. Orden de venta generada: %s.',
                rec.sale_order_id.name or '—'
            ))

    def action_create_sale_order(self):
        """Convert an allocated/notified reservation to a real sale.order."""
        self.ensure_one()
        if self.status not in ('allocated', 'notified'):
            raise UserError(_('Solo se puede generar venta desde estado "Stock Asignado" o "Notificado".'))

        order = self.env['sale.order'].create({
            'partner_id': self.customer_id.id,
            'warehouse_id': self.warehouse_id.id,
            'note': _('Generada desde reserva de preventa %s') % self.name,
            'order_line': [(0, 0, {
                'product_id': self.product_id.id,
                'product_uom_qty': self.quantity,
                'price_unit': self.product_id.lst_price,
            })],
        })
        self.write({'sale_order_id': order.id})
        self.action_complete()

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_resend_email(self):
        """Reenvía el email de notificación de stock al cliente."""
        self.ensure_one()
        if self.status != 'notified':
            raise UserError(_('Solo se puede reenviar el email en estado "Pendiente de Pago".'))
        self._send_allocation_email()
        self.message_post(body=_('📧 Email de notificación reenviado al cliente (%s).', self.customer_id.email))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Email reenviado'),
                'message': _('Se reenvió la notificación a %s.') % self.customer_id.email,
                'type': 'success',
                'sticky': False,
            },
        }

    # ── Notificaciones ────────────────────────────────────────────────────────

    def _send_confirmation_email(self):
        self.ensure_one()
        template = self.env.ref(
            'geekz_preorders.email_template_preorder_confirmed',
            raise_if_not_found=False,
        )
        if template:
            template.send_mail(self.id, force_send=True, raise_exception=False)
        else:
            _logger.warning('Email template geekz_preorders.email_template_preorder_confirmed not found')

    def _send_allocation_email(self):
        self.ensure_one()
        template = self.env.ref(
            'geekz_preorders.email_template_preorder_allocated',
            raise_if_not_found=False,
        )
        if template:
            template.send_mail(self.id, force_send=True, raise_exception=False)
        else:
            _logger.warning('Email template geekz_preorders.email_template_preorder_allocated not found')

    # ── Lógica de asignación de stock ─────────────────────────────────────────

    def _trigger_reallocation(self):
        """After releasing stock, attempt to allocate to next FIFO customers."""
        product_ids = self.mapped('product_id').ids
        for pid in product_ids:
            self.env['preorder.reservation']._allocate_stock_for_product(pid)

    @api.model
    def _allocate_stock_for_product(self, product_id):
        """
        Thread-safe FIFO stock allocation.

        Uses SELECT FOR UPDATE SKIP LOCKED to prevent race conditions when
        multiple processes attempt allocation concurrently (e.g., multiple
        incoming shipments validated at the same time).
        """
        product = self.env['product.product'].browse(product_id)
        warehouse = self.env['stock.warehouse'].search(
            [('company_id', '=', self.env.company.id)], limit=1
        )
        if not warehouse:
            _logger.warning('No warehouse found for company %s', self.env.company.name)
            return

        available_qty = product.with_context(
            location=warehouse.lot_stock_id.id
        ).qty_available

        _logger.info(
            'Allocation run: product=%s [%d], qty_available=%.2f',
            product.display_name, product_id, available_qty,
        )

        if available_qty <= 0:
            return

        # Lock rows in FIFO order, skip rows locked by other transactions
        self.env.cr.execute("""
            SELECT id, quantity
            FROM preorder_reservation
            WHERE product_id = %s
              AND status = 'active'
              AND active = TRUE
            ORDER BY reservation_date ASC, id ASC
            FOR UPDATE SKIP LOCKED
        """, (product_id,))

        rows = self.env.cr.fetchall()
        if not rows:
            return

        allocated_total = 0.0
        to_allocate = []

        for row_id, qty in rows:
            if allocated_total + qty <= available_qty:
                to_allocate.append(row_id)
                allocated_total += qty
            else:
                break  # Preserve strict FIFO: don't skip anyone

        if not to_allocate:
            _logger.info('No reservations fit within available stock (%.2f)', available_qty)
            return

        reservations = self.browse(to_allocate)
        for res in reservations:
            try:
                with self.env.cr.savepoint():
                    res.action_allocate()
                    res.action_notify()
                _logger.info(
                    'Allocated reservation %s → customer=%s, qty=%d',
                    res.name, res.customer_id.name, res.quantity,
                )
            except Exception as exc:
                _logger.error('Error allocating reservation %s: %s', res.name, exc)

    # ── Cron Jobs ─────────────────────────────────────────────────────────────

    @api.model
    def _cron_expire_reservations(self):
        """Hourly: expire notified reservations past their expiration_date."""
        now = fields.Datetime.now()
        expired = self.search([
            ('status', '=', 'notified'),
            ('expiration_date', '<=', now),
        ])
        if expired:
            _logger.info('Cron expire: processing %d overdue reservations', len(expired))
            expired.action_expire()

    @api.model
    def _cron_notify_pending_allocated(self):
        """Hourly: re-notify any allocated reservation that missed its email."""
        pending = self.search([('status', '=', 'allocated')])
        for res in pending:
            try:
                res.action_notify()
            except Exception as exc:
                _logger.error('Cron notify: error on reservation %s: %s', res.name, exc)

    @api.model
    def _cron_remind_expiring_soon(self):
        """Hourly: send reminder email to reservations expiring within 24 hours."""
        now = fields.Datetime.now()
        deadline = now + timedelta(hours=24)
        expiring = self.search([
            ('status', '=', 'notified'),
            ('expiration_date', '>', now),
            ('expiration_date', '<=', deadline),
            ('reminder_sent', '=', False),
        ])
        for res in expiring:
            try:
                template = self.env.ref(
                    'geekz_preorders.email_template_preorder_reminder',
                    raise_if_not_found=False,
                )
                if template:
                    template.send_mail(res.id, force_send=True, raise_exception=False)
                res.write({'reminder_sent': True})
                res.message_post(body=_(
                    '⚠️ Recordatorio de vencimiento enviado al cliente. '
                    'La reserva expira en menos de 24 horas.'
                ))
                _logger.info('Reminder sent for reservation %s', res.name)
            except Exception as exc:
                _logger.error('Error sending reminder for reservation %s: %s', res.name, exc)

    @api.model
    def _cron_cleanup_old_reservations(self):
        """Monthly: archive terminal reservations older than 6 months."""
        cutoff = fields.Datetime.now() - timedelta(days=180)
        old = self.search([
            ('status', 'in', ('completed', 'cancelled', 'expired')),
            ('reservation_date', '<', cutoff),
        ])
        if old:
            old.write({'active': False})
            _logger.info('Cron cleanup: archived %d old reservations', len(old))
