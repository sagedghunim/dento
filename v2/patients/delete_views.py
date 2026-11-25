"""The owner deletes a patient's file, or brings a hidden one back (patients.services.delete / restore)."""

from core.api import Result, api

from . import services


def _replay(ctx, stored):
    return {"result": stored.get("result", "hidden")}


@api("POST", perm="patients.edit", replay=_replay)
def delete_view(request, ctx, data, patient_id):
    from finance.views import gate

    from core import sql

    name = sql.row(ctx, 'SELECT full_name FROM "patients_patient" WHERE clinic_id = %s AND id = %s', [ctx.clinic_id, patient_id])
    if name is None:
        from core.errors import ApiError
        from django.utils.translation import gettext as _

        raise ApiError("not_found", _("This patient wasn't found."), 404)
    waiting = gate(ctx, "patient", "patient", 0, name[0], {"patient_id": patient_id})
    if waiting:
        return waiting
    result = services.delete(ctx, patient_id)
    return Result({"result": result}, target_type="patient", target_id=patient_id)


@api("POST", perm="patients.edit", replay=lambda ctx, stored: {"result": "restored"})
def restore_view(request, ctx, data, patient_id):
    services.restore(ctx, patient_id)
    return Result({"result": "restored"}, target_type="patient", target_id=patient_id)
