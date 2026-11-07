"""Writing the change log and the access log (design 4.10, 8)."""

from django.utils import timezone

from .models import AuditAccess, AuditChange


def record_change(ctx, action, target_type="", target_id=None, before=None, after=None, **extra):
    details = {}
    if before is not None:
        details["before"] = before
    if after is not None:
        details["after"] = after
    details.update(extra)
    AuditChange.objects.create(
        clinic_id=ctx.clinic_id, actor_kind=ctx.actor, membership_id=ctx.membership_id, user_id=ctx.user_id,
        action=action, target_type=target_type, target_id=target_id, details=details, ip=ctx.ip,
        request_id=ctx.request_id, at=timezone.now(),
    )


def record_access(ctx, patient_id, action):
    """One row per read of clinical data. Views declare it with @api(access=...)."""
    ctx.require_member()
    AuditAccess.objects.create(
        clinic_id=ctx.clinic_id, at=timezone.now(), membership_id=ctx.membership_id, patient_id=patient_id,
        action=action, ip=ctx.ip, request_id=ctx.request_id,
    )
