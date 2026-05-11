from collections import defaultdict

from odoo import _, api, fields, models


class PreorderDashboard(models.TransientModel):
    _name = 'preorder.dashboard'
    _description = 'Dashboard KPI de Preventas GeekZ'

    # ── Contadores por estado ─────────────────────────────────────────────────

    active_count = fields.Integer(string='Activas', readonly=True)
    allocated_count = fields.Integer(string='Stock Asignado', readonly=True)
    notified_count = fields.Integer(string='Pendientes de Pago', readonly=True)
    completed_count = fields.Integer(string='Completadas', readonly=True)
    expired_count = fields.Integer(string='Expiradas', readonly=True)
    cancelled_count = fields.Integer(string='Canceladas', readonly=True)

    # ── Métricas de rendimiento ───────────────────────────────────────────────

    conversion_rate = fields.Float(
        string='Tasa de Conversión (%)',
        readonly=True,
        digits=(5, 1),
        help='Porcentaje de reservas terminales que resultaron en venta.',
    )
    avg_days_to_payment = fields.Float(
        string='Días promedio hasta pago',
        readonly=True,
        digits=(5, 1),
        help='Promedio de días entre asignación de stock y pago confirmado.',
    )
    pending_stock_units = fields.Integer(
        string='Unidades en cola de espera',
        readonly=True,
        help='Total de unidades solicitadas por reservas activas aún sin stock asignado.',
    )

    # ── Rankings (texto libre) ────────────────────────────────────────────────

    top_products_text = fields.Text(
        string='Top 5 productos más reservados',
        readonly=True,
    )
    top_expired_customers_text = fields.Text(
        string='Top 5 clientes con más expiraciones',
        readonly=True,
    )

    # ── Populate all KPIs on creation ─────────────────────────────────────────

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        Res = self.env['preorder.reservation']

        # Contadores por estado
        for status in ('active', 'allocated', 'notified', 'completed', 'expired', 'cancelled'):
            res[f'{status}_count'] = Res.search_count([('status', '=', status)])

        # Tasa de conversión: completadas / (completadas + expiradas + canceladas)
        terminal = res['completed_count'] + res['expired_count'] + res['cancelled_count']
        res['conversion_rate'] = (
            round(res['completed_count'] / terminal * 100, 1) if terminal else 0.0
        )

        # Tiempo promedio entre allocation_date y completion_date
        completed_recs = Res.search([
            ('status', '=', 'completed'),
            ('completion_date', '!=', False),
            ('allocation_date', '!=', False),
        ], limit=500)
        if completed_recs:
            total_seconds = sum(
                (r.completion_date - r.allocation_date).total_seconds()
                for r in completed_recs
            )
            res['avg_days_to_payment'] = round(total_seconds / len(completed_recs) / 86400, 1)

        # Unidades en espera (solo estado active)
        active_recs = Res.search([('status', '=', 'active')])
        res['pending_stock_units'] = sum(active_recs.mapped('quantity'))

        # Top productos más reservados (activas + asignadas + notificadas)
        open_recs = Res.search([('status', 'in', ('active', 'allocated', 'notified'))])
        product_stats = defaultdict(lambda: {'count': 0, 'qty': 0})
        for r in open_recs:
            key = r.product_tmpl_id.name
            product_stats[key]['count'] += 1
            product_stats[key]['qty'] += r.quantity

        top5 = sorted(product_stats.items(), key=lambda x: x[1]['qty'], reverse=True)[:5]
        if top5:
            res['top_products_text'] = '\n'.join(
                f"{i + 1}. {name} — {s['qty']} uds ({s['count']} reservas)"
                for i, (name, s) in enumerate(top5)
            )
        else:
            res['top_products_text'] = '— Sin reservas activas actualmente'

        # Top 5 clientes con más expiraciones
        top_expired = self.env['res.partner'].search(
            [('preorder_expired_count', '>', 0)],
            order='preorder_expired_count desc',
            limit=5,
        )
        if top_expired:
            res['top_expired_customers_text'] = '\n'.join(
                f"{i + 1}. {p.name} — {p.preorder_expired_count} exp. "
                f"(score: {p.preorder_score:.0%})"
                for i, p in enumerate(top_expired)
            )
        else:
            res['top_expired_customers_text'] = '— Ningún cliente con expiraciones registradas'

        return res

    # ── Acciones de navegación desde stat buttons ─────────────────────────────

    def _open_reservations(self, domain, name):
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'preorder.reservation',
            'view_mode': 'list,form,kanban',
            'domain': domain,
        }

    def action_open_active(self):
        return self._open_reservations([('status', '=', 'active')], _('Reservas Activas'))

    def action_open_allocated(self):
        return self._open_reservations([('status', '=', 'allocated')], _('Stock Asignado'))

    def action_open_notified(self):
        return self._open_reservations([('status', '=', 'notified')], _('Pendientes de Pago'))

    def action_open_completed(self):
        return self._open_reservations([('status', '=', 'completed')], _('Reservas Completadas'))

    def action_open_expired(self):
        return self._open_reservations([('status', '=', 'expired')], _('Reservas Expiradas'))

    def action_open_cancelled(self):
        return self._open_reservations([('status', '=', 'cancelled')], _('Reservas Canceladas'))

    def action_refresh(self):
        """Re-opens the dashboard with fresh KPI data."""
        return self.env.ref('geekz_preorders.action_preorder_dashboard').read()[0]
