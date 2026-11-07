"""Account endpoints in the directory (design 6.2). Sign-in, the code and sign-out take no request key."""

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from core.api import SESSION_ACTIVE, SESSION_MFA, api
from core.errors import ApiError
from core.validate import text

from . import services


@api("POST", member=False, key=False)
def login_view(request, data):
    return services.sign_in(request, text(data, "username", 150), text(data, "password", 200, strip=False))


@api("POST", member=False, key=False)
def code_view(request, data):
    return services.verify_code(request, text(data, "code", 20))


@api("POST", member=False, key=False)
def logout_view(request, data):
    return services.sign_out(request)


@api("GET", member=False)
def me_view(request, data):
    from directory.services import clinics_for_user

    user = request.user
    if not user.is_authenticated:
        raise ApiError("signed_out", _("Please sign in."), 401)
    expires = timezone.now() + timezone.timedelta(seconds=request.session.get_expiry_age())
    return {
        "user": {"id": user.id, "username": user.username, "name": user.full_name, "language": user.language,
                 "must_change_password": user.must_change_password, "has_totp": user.has_totp,
                 "is_platform_admin": user.is_platform_admin},
        "clinics": clinics_for_user(user.id),
        "active": request.session.get(SESSION_ACTIVE),
        "mfa_verified": bool(request.session.get(SESSION_MFA)),
        "session_expires_at": expires.isoformat(timespec="seconds"),
        "app_version": settings.APP_VERSION,
    }


@api("POST", member=False)
def pick_clinic_view(request, data):
    from core.numbers import parse_int

    return services.pick_clinic(request, request.user, parse_int(data.get("membership_id"), "membership_id", 1, 2**62))


@api("POST", member=False, allow_password_change=True)
def password_view(request, data):
    return services.change_password(request, request.user, text(data, "current", 200, strip=False),
                                    text(data, "new", 200, strip=False))


@api("POST", member=False)
def language_view(request, data):
    language = text(data, "language", 2)
    if language not in ("ar", "en"):
        raise ApiError("invalid", _("Pick Arabic or English."), fields={"language": "choice"})
    request.user.language = language
    request.user.save(update_fields=["language"])
    return {"language": language}


@api("GET", member=False)
def totp_setup_view(request, data):
    user = request.user
    if not user.is_authenticated:
        raise ApiError("signed_out", _("Please sign in."), 401)
    if user.must_change_password:
        raise ApiError("password_change_required", _("Change your password to continue."), 403)
    return services.totp_setup(user)


@api("POST", member=False)
def totp_confirm_view(request, data):
    return services.totp_confirm(request, request.user, text(data, "code", 10))
