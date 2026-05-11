"""
Tests de flujos de negocio del módulo geekz_preorders.

Cubre:
- Creación y activación de reservas
- Límites por cliente (máximo 5 activas)
- Bloqueo de cliente por expiraciones acumuladas
- Flujo completo: active → allocated → notified → completed
- Cancelación y reasignación
- Generación de sale.order desde reserva
"""
from datetime import timedelta

from odoo.exceptions import UserError
from odoo.fields import Datetime
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'geekz_preorders')
class TestPreorderFlows(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Producto con preventa habilitada
        cls.product_tmpl = cls.env['product.template'].create({
            'name': 'Figura Test Preventa',
            'type': 'product',
            'preorder_enabled': True,
            'preorder_max_qty_per_customer': 3,
            'preorder_eta': '2025-12-01',
            'list_price': 29990,
        })
        cls.product = cls.product_tmpl.product_variant_ids[0]

        # Clientes
        cls.partner_a = cls.env['res.partner'].create({
            'name': 'Cliente A Test',
            'email': 'cliente_a@test.cl',
            'customer_rank': 1,
        })
        cls.partner_b = cls.env['res.partner'].create({
            'name': 'Cliente B Test',
            'email': 'cliente_b@test.cl',
            'customer_rank': 1,
        })
        cls.partner_blocked = cls.env['res.partner'].create({
            'name': 'Cliente Bloqueado Test',
            'email': 'blocked@test.cl',
            'customer_rank': 1,
            'preorder_blocked': True,
        })

        cls.Reservation = cls.env['preorder.reservation']

    def _create_reservation(self, partner, product=None, quantity=1, status='active'):
        product = product or self.product
        res = self.Reservation.create({
            'customer_id': partner.id,
            'product_id': product.id,
            'quantity': quantity,
        })
        if status == 'active':
            res.action_activate()
        return res

    # ── Test: Creación básica ─────────────────────────────────────────────────

    def test_create_reservation_generates_sequence(self):
        res = self._create_reservation(self.partner_a)
        self.assertRegex(res.name, r'^PRE/\d{4}/\d+$')

    def test_create_reservation_sets_reservation_date(self):
        before = Datetime.now()
        res = self._create_reservation(self.partner_a)
        self.assertGreaterEqual(res.reservation_date, before)

    def test_initial_status_is_draft(self):
        res = self.Reservation.create({
            'customer_id': self.partner_a.id,
            'product_id': self.product.id,
            'quantity': 1,
        })
        self.assertEqual(res.status, 'draft')

    # ── Test: Transiciones de estado ──────────────────────────────────────────

    def test_full_flow_draft_to_completed(self):
        res = self._create_reservation(self.partner_a)
        self.assertEqual(res.status, 'active')

        res.action_allocate()
        self.assertEqual(res.status, 'allocated')
        self.assertIsNotNone(res.allocation_date)

        res.action_notify()
        self.assertEqual(res.status, 'notified')
        self.assertIsNotNone(res.expiration_date)

        # Simular order creada
        res.action_create_sale_order()
        self.assertEqual(res.status, 'completed')
        self.assertIsNotNone(res.completion_date)
        self.assertIsNotNone(res.sale_order_id)

    def test_invalid_transition_raises_error(self):
        res = self._create_reservation(self.partner_a)
        # No se puede pasar de 'active' a 'completed' directamente
        with self.assertRaises(UserError):
            res.action_complete()

    def test_cancel_from_active(self):
        res = self._create_reservation(self.partner_a)
        res.action_cancel()
        self.assertEqual(res.status, 'cancelled')

    def test_cancel_from_notified(self):
        res = self._create_reservation(self.partner_a)
        res.action_allocate()
        res.action_notify()
        res.action_cancel()
        self.assertEqual(res.status, 'cancelled')

    def test_cannot_cancel_completed(self):
        res = self._create_reservation(self.partner_a)
        res.action_allocate()
        res.action_notify()
        res.action_create_sale_order()
        with self.assertRaises(UserError):
            res.action_cancel()

    # ── Test: Límites por cliente ─────────────────────────────────────────────

    def test_max_active_reservations_per_customer(self):
        """Cliente no puede tener más de 5 reservas activas."""
        products = []
        for i in range(5):
            tmpl = self.env['product.template'].create({
                'name': f'Producto Límite {i}',
                'type': 'product',
                'preorder_enabled': True,
            })
            products.append(tmpl.product_variant_ids[0])

        for p in products:
            self._create_reservation(self.partner_b, product=p)

        # La sexta debe fallar
        extra_tmpl = self.env['product.template'].create({
            'name': 'Producto Extra',
            'type': 'product',
            'preorder_enabled': True,
        })
        extra_product = extra_tmpl.product_variant_ids[0]
        res = self.Reservation.create({
            'customer_id': self.partner_b.id,
            'product_id': extra_product.id,
            'quantity': 1,
        })
        with self.assertRaises(UserError):
            res.action_activate()

    # ── Test: Cliente bloqueado ───────────────────────────────────────────────

    def test_blocked_customer_cannot_activate(self):
        res = self.Reservation.create({
            'customer_id': self.partner_blocked.id,
            'product_id': self.product.id,
            'quantity': 1,
        })
        with self.assertRaises(UserError):
            res.action_activate()

    def test_customer_blocked_after_3_expirations(self):
        partner = self.env['res.partner'].create({
            'name': 'Cliente a Bloquear',
            'email': 'tobloc@test.cl',
            'customer_rank': 1,
        })
        # Simular 3 expiraciones
        for _ in range(3):
            partner.sudo()._preorder_increment_expired()

        self.assertTrue(partner.preorder_blocked)

    def test_unblock_customer(self):
        self.partner_blocked.action_preorder_unblock()
        self.assertFalse(self.partner_blocked.preorder_blocked)

    # ── Test: Expiración ──────────────────────────────────────────────────────

    def test_expire_reservation(self):
        res = self._create_reservation(self.partner_a)
        res.action_allocate()
        res.action_notify()
        res.action_expire()
        self.assertEqual(res.status, 'expired')

    def test_cron_expires_overdue_reservations(self):
        res = self._create_reservation(self.partner_a)
        res.action_allocate()
        res.write({'status': 'notified'})
        # Poner expiración en el pasado
        res.write({'expiration_date': Datetime.now() - timedelta(hours=1)})

        self.Reservation._cron_expire_reservations()
        self.assertEqual(res.status, 'expired')

    def test_expired_increments_customer_counter(self):
        partner = self.env['res.partner'].create({
            'name': 'Cliente Expiración Counter',
            'email': 'exp@test.cl',
            'customer_rank': 1,
        })
        initial = partner.preorder_expired_count
        res = self._create_reservation(partner)
        res.action_allocate()
        res.action_notify()
        res.action_expire()
        self.assertEqual(partner.preorder_expired_count, initial + 1)

    # ── Test: Score del cliente ───────────────────────────────────────────────

    def test_partner_score_perfect(self):
        partner = self.env['res.partner'].create({
            'name': 'Cliente Score Test',
            'email': 'score@test.cl',
            'customer_rank': 1,
        })
        partner._preorder_increment_completed()
        partner._preorder_increment_completed()
        self.assertAlmostEqual(partner.preorder_score, 1.0)

    def test_partner_score_mixed(self):
        partner = self.env['res.partner'].create({
            'name': 'Cliente Score Mixto',
            'email': 'mix@test.cl',
            'customer_rank': 1,
        })
        partner._preorder_increment_completed()
        partner._preorder_increment_expired()
        # 1 completada, 1 expirada → score = 0.5
        self.assertAlmostEqual(partner.preorder_score, 0.5)

    # ── Test: Cantidad ────────────────────────────────────────────────────────

    def test_zero_quantity_raises_constraint(self):
        with self.assertRaises(Exception):
            self.Reservation.create({
                'customer_id': self.partner_a.id,
                'product_id': self.product.id,
                'quantity': 0,
            })

    def test_negative_quantity_raises_constraint(self):
        with self.assertRaises(Exception):
            self.Reservation.create({
                'customer_id': self.partner_a.id,
                'product_id': self.product.id,
                'quantity': -1,
            })
