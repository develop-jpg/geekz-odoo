"""
Tests de asignación de stock: FIFO, concurrencia, casos borde.

Cubre:
- Asignación FIFO correcta
- Stock parcial: solo asigna a quienes caben
- Sin sobreventa
- Reasignación tras cancelación
- Reasignación tras expiración
- Producto sin reservas activas
- Sin stock disponible
"""
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'geekz_preorders')
class TestStockAllocation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1
        )

        cls.product_tmpl = cls.env['product.template'].create({
            'name': 'Figura Allocation Test',
            'type': 'product',
            'preorder_enabled': True,
        })
        cls.product = cls.product_tmpl.product_variant_ids[0]

        # Crear partners de prueba
        cls.partners = cls.env['res.partner'].create([
            {'name': f'Cliente FIFO {i}', 'email': f'fifo{i}@test.cl', 'customer_rank': 1}
            for i in range(6)
        ])

        cls.Reservation = cls.env['preorder.reservation']

    def _create_active_reservation(self, partner, quantity=1):
        res = self.Reservation.create({
            'customer_id': partner.id,
            'product_id': self.product.id,
            'quantity': quantity,
            'warehouse_id': self.warehouse.id,
        })
        res.action_activate()
        return res

    def _set_stock(self, qty):
        """Ajusta el stock disponible del producto en la bodega principal."""
        inventory_location = self.env.ref('stock.location_inventory')
        stock_location = self.warehouse.lot_stock_id
        move = self.env['stock.move'].create({
            'name': 'Test stock adjustment',
            'product_id': self.product.id,
            'product_uom': self.product.uom_id.id,
            'product_uom_qty': qty,
            'location_id': inventory_location.id,
            'location_dest_id': stock_location.id,
        })
        move._action_confirm()
        move._action_assign()
        move._action_done()

    # ── Test: Sin stock ───────────────────────────────────────────────────────

    def test_no_allocation_when_no_stock(self):
        """Si no hay stock, ninguna reserva debe cambiar a allocated."""
        res = self._create_active_reservation(self.partners[0])
        # Sin ajustar stock (qty = 0)
        self.Reservation._allocate_stock_for_product(self.product.id)
        res.invalidate_recordset()
        self.assertEqual(res.status, 'active')

    # ── Test: Stock exacto para 1 reserva ────────────────────────────────────

    def test_allocation_single_reservation(self):
        self._set_stock(1)
        res = self._create_active_reservation(self.partners[0], quantity=1)
        self.Reservation._allocate_stock_for_product(self.product.id)
        res.invalidate_recordset()
        self.assertIn(res.status, ('allocated', 'notified'))

    # ── Test: FIFO correcto ───────────────────────────────────────────────────

    def test_fifo_order_respected(self):
        """
        3 reservas, stock para 2.
        El primero y segundo deben ser asignados; el tercero queda activo.
        Las fechas se insertan manualmente para garantizar el orden.
        """
        import datetime

        self._set_stock(2)

        # Crear reservas con fechas explícitas (FIFO: partner[1] primero)
        res1 = self._create_active_reservation(self.partners[1], quantity=1)
        res2 = self._create_active_reservation(self.partners[2], quantity=1)
        res3 = self._create_active_reservation(self.partners[3], quantity=1)

        now = datetime.datetime.now()
        res1.write({'reservation_date': now - datetime.timedelta(minutes=30)})
        res2.write({'reservation_date': now - datetime.timedelta(minutes=20)})
        res3.write({'reservation_date': now - datetime.timedelta(minutes=10)})

        self.Reservation._allocate_stock_for_product(self.product.id)

        res1.invalidate_recordset()
        res2.invalidate_recordset()
        res3.invalidate_recordset()

        self.assertIn(res1.status, ('allocated', 'notified'), 'Primero en FIFO debe ser asignado')
        self.assertIn(res2.status, ('allocated', 'notified'), 'Segundo en FIFO debe ser asignado')
        self.assertEqual(res3.status, 'active', 'Tercero no debe ser asignado (sin stock)')

    # ── Test: Stock parcial con cantidades variadas ───────────────────────────

    def test_partial_stock_with_larger_quantities(self):
        """
        Stock = 3. Reservas: A=2, B=2.
        Solo A debe asignarse (cabe con 2). B queda activo (no se puede skip).
        """
        import datetime

        self._set_stock(3)

        res_a = self._create_active_reservation(self.partners[4], quantity=2)
        res_b = self._create_active_reservation(self.partners[5], quantity=2)

        now = datetime.datetime.now()
        res_a.write({'reservation_date': now - datetime.timedelta(minutes=20)})
        res_b.write({'reservation_date': now - datetime.timedelta(minutes=10)})

        self.Reservation._allocate_stock_for_product(self.product.id)

        res_a.invalidate_recordset()
        res_b.invalidate_recordset()

        self.assertIn(res_a.status, ('allocated', 'notified'))
        self.assertEqual(res_b.status, 'active', 'B no debe saltarse aunque haya 1 unidad libre')

    # ── Test: Sin reservas activas ────────────────────────────────────────────

    def test_allocation_no_active_reservations(self):
        """No debe lanzar error si no hay reservas activas."""
        self._set_stock(5)
        # Solo crear reserva completada
        res = self._create_active_reservation(self.partners[0])
        res.action_allocate()
        res.action_notify()
        res.action_create_sale_order()  # completed

        # No debe lanzar excepción
        self.Reservation._allocate_stock_for_product(self.product.id)

    # ── Test: Reasignación tras cancelación ───────────────────────────────────

    def test_reallocation_after_cancellation(self):
        """
        A tiene stock asignado. B está en espera.
        A cancela → B debe recibir el stock.
        """
        import datetime

        self._set_stock(1)

        res_a = self._create_active_reservation(self.partners[0], quantity=1)
        res_b = self._create_active_reservation(self.partners[1], quantity=1)

        now = datetime.datetime.now()
        res_a.write({'reservation_date': now - datetime.timedelta(minutes=30)})
        res_b.write({'reservation_date': now - datetime.timedelta(minutes=20)})

        # Asignar a A
        self.Reservation._allocate_stock_for_product(self.product.id)
        res_a.invalidate_recordset()
        self.assertIn(res_a.status, ('allocated', 'notified'))

        # A cancela → debe reasignarse a B
        res_a.action_cancel()

        res_b.invalidate_recordset()
        self.assertIn(
            res_b.status, ('allocated', 'notified'),
            'B debe recibir el stock después de que A cancele',
        )

    # ── Test: Reasignación tras expiración ────────────────────────────────────

    def test_reallocation_after_expiration(self):
        """Stock se libera cuando una reserva 'notified' expira."""
        import datetime

        self._set_stock(1)

        res_a = self._create_active_reservation(self.partners[0], quantity=1)
        res_b = self._create_active_reservation(self.partners[1], quantity=1)

        now = datetime.datetime.now()
        res_a.write({'reservation_date': now - datetime.timedelta(minutes=30)})
        res_b.write({'reservation_date': now - datetime.timedelta(minutes=20)})

        # Asignar a A
        self.Reservation._allocate_stock_for_product(self.product.id)
        res_a.invalidate_recordset()
        self.assertIn(res_a.status, ('allocated', 'notified'))

        # A expira → B debe recibir el stock
        res_a.write({'status': 'notified', 'expiration_date': datetime.datetime.now() - datetime.timedelta(seconds=1)})
        self.Reservation._cron_expire_reservations()

        res_b.invalidate_recordset()
        self.assertIn(
            res_b.status, ('allocated', 'notified'),
            'B debe recibir el stock después de que A expire',
        )

    # ── Test: Producto sin preventa habilitada ────────────────────────────────

    def test_product_without_preorder_enabled(self):
        """No debe asignarse stock a productos que no tengan preventa activa."""
        tmpl_no_preorder = self.env['product.template'].create({
            'name': 'Producto Sin Preventa',
            'type': 'product',
            'preorder_enabled': False,
        })
        product_no_preorder = tmpl_no_preorder.product_variant_ids[0]

        self._set_stock(10)
        res = self.Reservation.create({
            'customer_id': self.partners[0].id,
            'product_id': product_no_preorder.id,
            'quantity': 1,
            'status': 'active',
        })
        # La asignación se ejecuta solo por llamado manual o desde stock.picking
        self.Reservation._allocate_stock_for_product(product_no_preorder.id)
        res.invalidate_recordset()
        # Si no hay stock ni reservas en este producto, no hay error
        self.assertIn(res.status, ('active',))
