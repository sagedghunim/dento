"""The owner deletes a patient's file, or brings a hidden one back (patients.services.delete / restore)."""

from core.api import Result, api

from . import services


def _replay(ctx, stored):
    return {"result": stored.get("result", "hidden")}


@api("POST", perm="patients.edit", replay=_replay)
def delete_view(request, ctx, data, patient_id):
    result = services.delete(ctx, patient_id)
    return Result({"result": result}, target_type="patient", target_id=patient_id)


@api("POST", perm="patients.edit", replay=lambda ctx, stored: {"result": "restored"})
def restore_view(request, ctx, data, patient_id):
    services.restore(ctx, patient_id)
    return Result({"result": "restored"}, target_type="patient", target_id=patient_id)
