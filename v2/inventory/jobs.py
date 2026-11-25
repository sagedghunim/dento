"""Auto-send of prepared orders (design H1 flow 4): the owner's switch `orders_auto_send`, off by default.

Nothing runs while the WhatsApp provider is unset (core.whatsapp). After the refill commits, a job claims the draft
with the conditional draft -> sent update (one claim wins), commits, then calls the provider outside any
transaction. If the provider fails, the order goes back to draft (conditional sent -> draft) and is audited, so the
desk sends it by hand.
"""

import logging

from django.utils import timezone
from procrastinate.contrib.django import app

from audit.services import record_change
from clinics.models import Clinic
from core import sql, whatsapp
from core.context import Ctx
from core.routing import clinic_transaction

log = logging.getLogger(__name__)


def queue_auto_send(clinic_id, alias, order_id):
    try:
        auto_send_task.defer(clinic_id=clinic_id, alias=alias, order_id=order_id)
    except Exception:  # the desk still sees the draft and sends it by hand
        log.exception("auto-send not queued: clinic=%s order=%s", clinic_id, order_id)


def auto_send(clinic_id, alias, order_id):
    """Returns "sent", "failed" or "skipped"."""
    from . import orders

    with clinic_transaction(clinic_id, alias):
        clinic = Clinic.objects.filter(id=clinic_id).first()
        if clinic is None or not clinic.settings.get("orders_auto_send") or not whatsapp.configured():
            return "skipped"
        ctx = Ctx(clinic=clinic, membership=None, user_id=None, actor="system", codes=frozenset(), alias=alias)
        claimed = sql.row(ctx, """
            UPDATE "inventory_order" SET status = 'sent', sent_at = %s WHERE clinic_id = %s AND id = %s
               AND status = 'draft' RETURNING id""", [timezone.now(), clinic_id, order_id])
        if claimed is None:
            return "skipped"
        order = orders.get_order(ctx, order_id)
        text = orders.message(ctx, order)
        record_change(ctx, "order.send", "order", order_id, after={"supplier_id": order["supplier_id"], "auto": True})
    try:
        whatsapp.send(order["supplier_phone"], "purchase_order", {"text": text})
        return "sent"
    except Exception:
        log.exception("auto-send failed: clinic=%s order=%s", clinic_id, order_id)
    with clinic_transaction(clinic_id, alias):
        # Back to draft, unless a new draft for this supplier opened meanwhile (one draft each): then it stays sent
        # and the desk uses "send again" on it.
        back = sql.row(ctx, """
            UPDATE "inventory_order" o SET status = 'draft', sent_at = NULL WHERE clinic_id = %s AND id = %s
               AND status = 'sent' AND NOT EXISTS (SELECT 1 FROM "inventory_order" d WHERE d.clinic_id = o.clinic_id
                                                    AND d.supplier_id = o.supplier_id AND d.status = 'draft')
            RETURNING id""", [clinic_id, order_id])
        record_change(ctx, "order.send_failed", "order", order_id, back_to_draft=back is not None)
    return "failed"


@app.task(name="inventory.auto_send", pass_context=False)
def auto_send_task(clinic_id, alias, order_id):
    return auto_send(clinic_id, alias, order_id)
