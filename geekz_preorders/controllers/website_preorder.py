import logging

from odoo import _
from odoo.exceptions import UserError, ValidationError
from odoo.http import Controller, request, route
from odoo.addons.portal.controllers.portal import CustomerPortal

_logger = logging.getLogger(__name__)


class PreorderController(Controller):
    """
    HTTP routes para el sistema de preventas GeekZ.

    Rutas:
        POST /preorder/reserve          — Crear reserva desde website_sale
        GET  /preorder/my               — Lista de reservas del cliente (portal)
        POST /preorder/cancel/<id>      — Cancelar reserva propia
        GET  /preorder/checkout/<id>    — Convertir reserva a carrito y redirigir
    """

    # ── Crear reserva ─────────────────────────────────────────────────────────

    @route('/preorder/reserve', type='http', auth='user', methods=['POST'], website=True, csrf=True)
    def create_reservation(self, product_id=None, quantity=1, **kwargs):
        """
        Crea una reserva de preventa para el producto indicado.
        El usuario debe estar autenticado (auth='user').
        """
        try:
            product_id = int(product_id)
            quantity = max(1, int(quantity))
        except (TypeError, ValueError):
            return request.redirect('/shop?preorder_error=invalid_product')

        partner = request.env.user.partner_id
        Reservation = request.env['preorder.reservation'].sudo()

        # Validar que el producto existe y tiene preventa habilitada
        product = request.env['product.product'].sudo().browse(product_id)
        if not product.exists() or not product.preorder_enabled:
            return request.redirect('/shop?preorder_error=not_preorder')

        # Validar límite de cantidad por cliente para este producto
        max_qty = product.preorder_max_qty_per_customer or 1
        if quantity > max_qty:
            quantity = max_qty

        # Verificar bloqueo y límites antes de crear
        if partner.preorder_blocked:
            return request.redirect(
                '/preorder/my?error=blocked'
            )

        active_count = Reservation.search_count([
            ('customer_id', '=', partner.id),
            ('status', 'in', ('active', 'allocated', 'notified')),
        ])
        if active_count >= 5:
            return request.redirect('/preorder/my?error=limit_reached')

        # Verificar límite total del producto
        tmpl = product.product_tmpl_id
        if tmpl.preorder_limit_total > 0:
            total_reserved = Reservation.search_count([
                ('product_tmpl_id', '=', tmpl.id),
                ('status', 'in', ('active', 'allocated', 'notified')),
            ])
            if total_reserved >= tmpl.preorder_limit_total:
                return request.redirect(
                    '/shop?preorder_error=product_full&product=%d' % tmpl.id
                )

        try:
            reservation = Reservation.create({
                'customer_id': partner.id,
                'product_id': product_id,
                'quantity': quantity,
                'status': 'active',
            })
            # Enviar email de confirmación
            template = request.env.ref(
                'geekz_preorders.email_template_preorder_confirmed',
                raise_if_not_found=False,
            )
            if template:
                template.sudo().send_mail(reservation.id, force_send=True, raise_exception=False)

            _logger.info(
                'New preorder reservation %s: customer=%s, product=%s, qty=%d',
                reservation.name, partner.name, product.name, quantity,
            )
        except (UserError, ValidationError) as exc:
            _logger.warning('Error creating preorder reservation: %s', exc)
            return request.redirect('/preorder/my?error=create_failed')

        return request.render('geekz_preorders.preorder_confirmed_page', {
            'reservation': reservation,
        })

    # ── Listado de reservas del cliente ───────────────────────────────────────

    @route('/preorder/my', type='http', auth='user', website=True)
    def my_preorders(self, **kwargs):
        """Portal del cliente: muestra todas sus reservas."""
        partner = request.env.user.partner_id
        reservations = request.env['preorder.reservation'].sudo().search([
            ('customer_id', '=', partner.id),
            ('active', '=', True),
        ], order='reservation_date desc')

        return request.render('geekz_preorders.portal_my_preorders', {
            'reservations': reservations,
            'error': kwargs.get('error'),
        })

    # ── Cancelar reserva ──────────────────────────────────────────────────────

    @route('/preorder/cancel/<int:reservation_id>', type='http', auth='user',
           methods=['POST'], website=True, csrf=True)
    def cancel_reservation(self, reservation_id, **kwargs):
        """Permite al cliente cancelar su propia reserva activa."""
        partner = request.env.user.partner_id
        reservation = request.env['preorder.reservation'].sudo().search([
            ('id', '=', reservation_id),
            ('customer_id', '=', partner.id),
            ('status', 'in', ('draft', 'active', 'allocated', 'notified')),
        ], limit=1)

        if not reservation:
            return request.redirect('/preorder/my?error=not_found')

        try:
            reservation.action_cancel()
            _logger.info(
                'Reservation %s cancelled by customer %s',
                reservation.name, partner.name,
            )
        except (UserError, ValidationError) as exc:
            _logger.warning('Error cancelling reservation %d: %s', reservation_id, exc)
            return request.redirect('/preorder/my?error=cancel_failed')

        return request.redirect('/preorder/my')

    # ── Checkout: convertir reserva a carrito ─────────────────────────────────

    @route('/preorder/checkout/<int:reservation_id>', type='http', auth='user', website=True)
    def preorder_checkout(self, reservation_id, **kwargs):
        """
        Convierte la reserva en un sale.order y redirige al checkout normal.
        Solo funciona para reservas en estado 'notified' del cliente autenticado.
        """
        partner = request.env.user.partner_id
        reservation = request.env['preorder.reservation'].sudo().search([
            ('id', '=', reservation_id),
            ('customer_id', '=', partner.id),
            ('status', '=', 'notified'),
        ], limit=1)

        if not reservation:
            return request.redirect('/preorder/my?error=not_found')

        try:
            result = reservation.action_create_sale_order()
            order_id = result.get('res_id')
            if order_id:
                # Vincular el sale.order a la sesión del website como carrito
                order = request.env['sale.order'].sudo().browse(order_id)
                request.session['sale_order_id'] = order.id
                return request.redirect('/shop/checkout')
        except (UserError, ValidationError) as exc:
            _logger.error(
                'Error creating sale.order from reservation %d: %s',
                reservation_id, exc,
            )
            return request.redirect('/preorder/my?error=checkout_failed')

        return request.redirect('/preorder/my')


class PreorderPortalController(CustomerPortal):
    """
    Extiende el portal home para inyectar el contador de reservas activas
    en el sidebar del cliente. Requiere el template portal_my_preorders_entry
    que usa placeholder_count="preorder_count".
    """

    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'preorder_count' in counters:
            partner = request.env.user.partner_id
            values['preorder_count'] = request.env['preorder.reservation'].sudo().search_count([
                ('customer_id', '=', partner.id),
                ('status', 'in', ('active', 'allocated', 'notified')),
            ])
        return values
