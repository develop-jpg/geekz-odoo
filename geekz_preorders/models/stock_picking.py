import logging

from odoo import models

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    def _action_done(self):
        """
        Hook post-validación: al confirmar una recepción de mercadería,
        disparar la asignación FIFO de preventas para los productos recibidos.

        Se usa _action_done (no button_validate) porque button_validate puede
        retornar un wizard de backorder sin completar la transferencia.
        _action_done siempre se ejecuta cuando la transferencia queda en 'done'.
        """
        result = super()._action_done()

        # Solo recepciones (compra → bodega), ya completadas
        incoming = self.filtered(
            lambda p: p.picking_type_code == 'incoming' and p.state == 'done'
        )
        if not incoming:
            return result

        # Recolectar productos únicos de todos los movimientos completados
        product_ids = incoming.move_ids.filtered(
            lambda m: m.state == 'done'
        ).product_id.ids

        if not product_ids:
            return result

        _logger.info(
            'GeekZ Preventas: recepción validada, disparando asignación '
            'para %d producto(s): %s',
            len(product_ids), product_ids,
        )

        Reservation = self.env['preorder.reservation']
        for pid in product_ids:
            try:
                Reservation._allocate_stock_for_product(pid)
            except Exception as exc:
                _logger.error(
                    'GeekZ Preventas: error en asignación para producto %d: %s',
                    pid, exc,
                )

        return result
