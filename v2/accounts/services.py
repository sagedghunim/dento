"""Signing in, two-factor codes, passwords and picking a clinic (design 5.9)."""

import datetime as dt
import io

import segno
from django.contrib.auth import login, logout, password_validation, update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.api import SESSION_ACTIVE, SESSION_MFA, client_ip
from core.errors import ApiError
from core.text import looks_like_phone, normalize_phone

from . import crypto, throttle, totp
from .models import AccountEvent, RecoveryCode, User

PENDING_USER = "hamada_pending_user"
PENDING_UNTIL = "hamada_pending_until"
PENDING_FAILURES = "hamada_pending_failures"
PENDING_SECONDS = 300
_DUMMY_HASH = make_password("not-a-real-password-for-timing")


def event(user_id, kind, request=None):
    AccountEvent.objects.create(user_id=user_id, kind=kind, ip=client_ip(request) if request else None)


def _find_user(identifier):
    identifier = (identifier or "").strip()
    if looks_like_phone(identifier):
        return User.objects.filter(phone=normalize_phone(identifier)).first()
    return User.objects.filter(username=identifier.lower()).first()


def _wrong(request, keys, user_id):
    throttle.record_failure(keys)
    event(user_id, "sign_in_failed", request)
    raise ApiError("invalid_credentials", _("The username or password is wrong."), 401)


def sign_in(request, identifier, password):
    ip = client_ip(request) or "-"
    keys = throttle.keys_for(identifier, ip)
    wait = throttle.wait_seconds(keys)
    if wait:
        minutes = max(1, round(wait / 60))
        raise ApiError("too_many_attempts", _("Too many attempts. Try again in %(minutes)s minutes.") % {"minutes": minutes},
                       429, retry_after=wait)
    user = _find_user(identifier)
    if user is None:
        check_password(password, _DUMMY_HASH)  # same time as a real check
        _wrong(request, keys, None)
    if not user.check_password(password) or not user.is_active:
        _wrong(request, keys, user.id)
    if user.must_change_password and user.temp_password_expires_at and user.temp_password_expires_at < timezone.now():
        raise ApiError("temp_password_expired", _("This temporary password has expired. Ask for a new one."), 401)
    if user.has_totp:
        request.session.cycle_key()
        request.session[PENDING_USER] = user.id
        request.session[PENDING_UNTIL] = (timezone.now() + dt.timedelta(seconds=PENDING_SECONDS)).timestamp()
        request.session[PENDING_FAILURES] = 0
        return {"next": "code"}
    _complete(request, user, mfa=False)
    return {"next": next_step(request, user)}


def _complete(request, user, mfa):
    for key in (PENDING_USER, PENDING_UNTIL, PENDING_FAILURES):
        request.session.pop(key, None)
    login(request, user)  # rotates the session key and the CSRF token
    request.session[SESSION_MFA] = mfa
    User.objects.filter(pk=user.pk).update(last_login=timezone.now())
    event(user.id, "sign_in", request)


def next_step(request, user):
    from directory.services import clinics_for_user

    if user.must_change_password:
        return "password"
    if user.is_platform_admin:
        return "platform" if user.has_totp else "security"
    clinics = clinics_for_user(user.id)
    if len(clinics) == 1 and not request.session.get(SESSION_ACTIVE):
        try:
            pick_clinic(request, user, clinics[0]["membership_id"])
        except ApiError:
            return "clinics"
    return "home" if request.session.get(SESSION_ACTIVE) else "clinics"


def verify_code(request, code):
    user_id = request.session.get(PENDING_USER)
    until = request.session.get(PENDING_UNTIL, 0)
    if not user_id or until < timezone.now().timestamp():
        for key in (PENDING_USER, PENDING_UNTIL, PENDING_FAILURES):
            request.session.pop(key, None)
        raise ApiError("signed_out", _("Your sign-in expired. Sign in again."), 401)
    user = User.objects.get(pk=user_id)
    if _accept_code(user, code):
        _complete(request, user, mfa=True)
        return {"next": next_step(request, user)}
    failures = request.session.get(PENDING_FAILURES, 0) + 1
    request.session[PENDING_FAILURES] = failures
    event(user.id, "totp_failed", request)
    if failures >= throttle.LIMITS["totp"]:
        for key in (PENDING_USER, PENDING_UNTIL, PENDING_FAILURES):
            request.session.pop(key, None)
        throttle.record_failure([("user", user.username)])
        raise ApiError("signed_out", _("Too many wrong codes. Sign in again."), 401)
    raise ApiError("invalid_code", _("That code is wrong. Try the newest one."), 400)


def _accept_code(user, code):
    """A TOTP code (each step once, even on two servers) or an unused recovery code."""
    cleaned = (code or "").strip()
    if len(cleaned.replace("-", "")) == 10:
        digest = crypto.hash_recovery_code(cleaned)
        return RecoveryCode.objects.filter(user=user, code_hash=digest, used_at__isnull=True).update(used_at=timezone.now()) == 1
    step = totp.verify(crypto.decrypt(user.totp_secret), cleaned, last_step=user.totp_last_step)
    if step is None:
        return False
    return User.objects.filter(pk=user.pk, totp_last_step__lt=step).update(totp_last_step=step) == 1


def sign_out(request):
    logout(request)
    return {"ok": True}


def change_password(request, user, current, new):
    if not user.check_password(current):
        raise ApiError("invalid_credentials", _("The current password is wrong."), 400, fields={"current": "wrong"})
    try:
        password_validation.validate_password(new, user)
    except ValidationError as error:
        raise ApiError("weak_password", " ".join(error.messages), 400, fields={"new": "weak"})
    user.set_password(new)
    user.must_change_password = False
    user.temp_password_expires_at = None
    user.save(update_fields=["password", "must_change_password", "temp_password_expires_at"])
    update_session_auth_hash(request, user)  # this session stays; the others are signed out
    event(user.id, "password_changed", request)
    return {"next": next_step(request, user)}


def _qr(user):
    """The secret and its QR code, for an authenticator app."""
    secret = crypto.decrypt(user.totp_secret)
    uri = totp.provisioning_uri(secret, user.username)
    buffer = io.BytesIO()
    # A full 4-module quiet zone on an opaque white background: phone scanners
    # fail on a thin margin or a transparent background in dark mode.
    qr = segno.make(uri, error="m")
    qr.save(buffer, kind="svg", scale=6, border=4, dark="#000", light="#fff", xmldecl=False, svgns=True)
    # segno writes width and height but no viewBox, so CSS sizing crops the code
    # instead of scaling it. A viewBox makes it scale to any size.
    size = qr.symbol_size(scale=6, border=4)[0]
    svg = buffer.getvalue().decode().replace("<svg ", f'<svg viewBox="0 0 {size} {size}" ', 1)
    return {"secret": secret, "qr_svg": svg}


def totp_setup(user):
    """Start or resume set-up: a new secret until a code confirms it."""
    if user.has_totp:
        raise ApiError("already_set_up", _("Two-step sign-in is already set up."), 409)
    if not user.totp_secret:
        user.totp_secret = crypto.encrypt(totp.new_secret())
        user.save(update_fields=["totp_secret"])
    return _qr(user)


def totp_show(request, user, password):
    """The code again, for another phone (Saged, 2026-11-22: the people working with him on the demo share the
    owner's sign-in). Only on a session that already passed two-step sign-in, and with the password typed again."""
    if not user.has_totp or not user.totp_secret:
        raise ApiError("not_set_up", _("Two-step sign-in isn't set up yet."), 409)
    if not request.session.get(SESSION_MFA):
        raise ApiError("mfa_required", _("Sign in with your code first."), 403)
    keys = throttle.keys_for(user.username, client_ip(request) or "-")
    wait = throttle.wait_seconds(keys)
    if wait:
        minutes = max(1, -(-wait // 60))
        raise ApiError("too_many_attempts", _("Too many attempts. Try again in %(minutes)s minutes.") % {"minutes": minutes},
                       429)
    if not user.check_password(password):
        throttle.record_failure(keys)
        raise ApiError("wrong_password", _("That password is wrong."), 400, fields={"password": "wrong"})
    event(user.id, "totp_shown", request)
    return _qr(user)


def totp_confirm(request, user, code):
    if user.has_totp:
        raise ApiError("already_set_up", _("Two-step sign-in is already set up."), 409)
    if not user.totp_secret:
        raise ApiError("not_started", _("Start the set-up first."), 409)
    step = totp.verify(crypto.decrypt(user.totp_secret), code)
    if step is None:
        raise ApiError("invalid_code", _("That code is wrong. Try the newest one."), 400)
    codes = [crypto.new_recovery_code() for _ in range(10)]
    with transaction.atomic(using="default"):
        User.objects.filter(pk=user.pk).update(totp_confirmed_at=timezone.now(), totp_last_step=step)
        RecoveryCode.objects.filter(user=user).delete()
        RecoveryCode.objects.bulk_create([RecoveryCode(user=user, code_hash=crypto.hash_recovery_code(c)) for c in codes])
    request.session[SESSION_MFA] = True
    event(user.id, "totp_set_up", request)
    return {"recovery_codes": codes}


def pick_clinic(request, user, membership_id):
    """Check the membership on its shard, then make it the session's active clinic."""
    from clinics.models import Membership
    from core.routing import clinic_transaction
    from directory.models import UserClinic
    from directory.services import shard_for

    link = UserClinic.objects.filter(user_id=user.id, membership_id=membership_id, is_active=True).first()
    if link is None:
        raise ApiError("not_found", _("That clinic isn't available."), 404)
    alias = shard_for(link.clinic_id, fresh=True)
    if alias is None:
        raise ApiError("not_found", _("That clinic isn't available."), 404)
    with clinic_transaction(link.clinic_id, alias):
        membership = (Membership.objects.select_related("clinic")
                      .filter(clinic_id=link.clinic_id, id=membership_id, user_id=user.id).first())
        if membership is None or not membership.is_active or not membership.clinic.owned or membership.clinic.status != "active":
            raise ApiError("not_found", _("That clinic isn't available."), 404)
    request.session[SESSION_ACTIVE] = {"clinic_id": link.clinic_id, "membership_id": membership_id}
    return {"clinic_id": link.clinic_id, "membership_id": membership_id}
