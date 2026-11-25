"""The owner brings back a file hidden before deleting a patient was taken out (Saged, 2026-11-26)."""

from core.api import Result, api

from . import services


@api("POST", perm="patients.edit", replay=lambda ctx, stored: {"result": "restored"})
def restore_view(request, ctx, data, patient_id):
    services.restore(ctx, patient_id)
    return Result({"result": "restored"}, target_type="patient", target_id=patient_id)
