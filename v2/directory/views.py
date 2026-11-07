"""Platform admin endpoints and page (design 6.2). No endpoint here returns patient data."""

from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from clinics.specialties import SPECIALTIES
from core.api import Result, api
from core.errors import ApiError
from core.numbers import parse_int
from core.validate import choice, text

from . import platform
from .models import DirectoryClinic


def _clinic_body(entry):
    return {"id": entry.id, "name": entry.name, "shard": entry.shard, "status": entry.status}


def _replay_clinic(user, stored):
    entry = DirectoryClinic.objects.filter(id=stored["target_id"]).first()
    # The temporary password is never stored, so a replay can't show it (design 5.12).
    return {"clinic": _clinic_body(entry) if entry else None, "temporary_password": None, "replayed": True}


@api("GET", member=False)
def clinics_list(request, data):
    platform.require_platform_admin(request)
    after = request.GET.get("after")
    return platform.clinic_rows(after=int(after) if after and after.isdigit() else None)


@api("POST", member=False, replay=_replay_clinic)
def clinics_create(request, data):
    platform.require_platform_admin(request)
    entry, owner, temporary = platform.create(
        name=text(data, "name", 120), specialty=choice(data, "specialty", list(SPECIALTIES)),
        city=text(data, "city", 60, required=False), owner_username=text(data, "owner_username", 150),
        owner_name=text(data, "owner_name", 120), owner_phone=text(data, "owner_phone", 20, required=False),
        language=choice(data, "language", ["ar", "en"]) if "language" in data else "ar",
    )
    return Result({"clinic": _clinic_body(entry), "owner": owner.username, "temporary_password": temporary},
                  status=201, target_type="clinic", target_id=entry.id)


def clinics_endpoint(request):
    """GET lists, POST creates (design 6.2: GET/POST platform/clinics)."""
    return clinics_create(request) if request.method == "POST" else clinics_list(request)


def _status_view(status):
    @api("POST", member=False, replay=_replay_clinic)
    def view(request, data, clinic_id):
        platform.require_platform_admin(request)
        entry = platform.set_status(parse_int(clinic_id, "clinic", 1, 2**62), status)
        return Result({"clinic": _clinic_body(entry)}, target_type="clinic", target_id=entry.id)

    view.__name__ = f"clinics_{status}"
    return view


clinics_suspend = _status_view(DirectoryClinic.Status.SUSPENDED)
clinics_reactivate = _status_view(DirectoryClinic.Status.ACTIVE)


@require_GET
@ensure_csrf_cookie
def platform_page(request):
    try:
        platform.require_platform_admin(request)
    except ApiError as error:
        if error.code == "signed_out":
            return redirect("/login?next=/platform")
        if error.code == "mfa_setup_required":
            return redirect("/account/security")
        if error.code == "mfa_required":
            return redirect("/login?step=code")
        if error.code == "password_change_required":
            return redirect("/account/password")
        return render(request, "web/forbidden.html", status=403)
    specialties = [(code, spec["name"].get(request.LANGUAGE_CODE, spec["name"]["en"])) for code, spec in SPECIALTIES.items()]
    return render(request, "web/platform.html", {"clinics": platform.clinic_rows()["items"], "specialties": specialties})
