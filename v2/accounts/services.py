"""Signing in, two-factor codes, passwords and picking a clinic (design 5.9)."""

import datetime as dt
import io

import segno
from django.contrib.auth import login, logout, password_validation, update_session_auth_hash
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
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
PENDING_SECONDS = 600  # ten minutes to fetch the code from the phone (Saged, 2026-11-24)
_DUMMY_HASH = make_password("not-a-real-password-for-timing")
# A device that passed the code is remembered for 30 days, so the code is asked once a month per phone or
# computer (Saged, 2026-11-25). The mark is signed and ends early when the password or the code's secret changes.
# Platform admins are always asked.
DEVICE_COOKIE = "hamada_device"
DEVICE_DAYS = 30
_DEVICE_SALT = "hamada.accounts.trusted-device"


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
    # Signing in by phone counts against the account too, so a phone number is no way around its limit.
    if user.username != (identifier or "").strip().lower():
        keys = keys + [k for k in throttle.keys_for(user.username, ip) if k[0] != "ip"]
        wait = throttle.wait_seconds(keys)
        if wait:
            minutes = max(1, round(wait / 60))
            raise ApiError("too_many_attempts", _("Too many attempts. Try again in %(minutes)s minutes.") % {"minutes": minutes},
                           429, retry_after=wait)
    if not user.check_password(password) or not user.is_active:
        _wrong(request, keys, user.id)
    if user.must_change_password and user.temp_password_expires_at and user.temp_password_expires_at < timezone.now():
        raise ApiError("temp_password_expired", _("This temporary password has expired. Ask for a new one."), 401)
    if user.has_totp and _trusted_device(request, user):
        _complete(request, user, mfa=True)
        event(user.id, "sign_in_device", request)
        return {"next": next_step(request, user)}
    if user.has_totp:
        request.session.cycle_key()
        request.session[PENDING_USER] = user.id
        request.session[PENDING_UNTIL] = (timezone.now() + dt.timedelta(seconds=PENDING_SECONDS)).timestamp()
        request.session[PENDING_FAILURES] = 0
        return {"next": "code"}
    _complete(request, user, mfa=False)
    return {"next": next_step(request, user)}


def _device_mark(user):
    """Changes when the password or the code's secret changes, so an old mark stops working."""
    from django.utils.crypto import salted_hmac

    return salted_hmac(_DEVICE_SALT, f"{user.password}|{user.totp_secret}|{user.totp_confirmed_at}").hexdigest()[:24]


def _trusted_device(request, user):
    if user.is_platform_admin:
        return False
    raw = request.COOKIES.get(DEVICE_COOKIE)
    if not raw:
        return False
    try:
        value = signing.loads(raw, salt=_DEVICE_SALT, max_age=DEVICE_DAYS * 24 * 3600)
    except signing.BadSignature:
        return False
    return isinstance(value, dict) and value.get("u") == user.id and value.get("m") == _device_mark(user)


def _remember_device(request, user):
    """The API answer sets the mark (core.api writes request.hamada_cookies)."""
    if user.is_platform_admin:
        return
    value = signing.dumps({"u": user.id, "m": _device_mark(user)}, salt=_DEVICE_SALT)
    request.hamada_cookies = [(DEVICE_COOKIE, value, {
        "max_age": DEVICE_DAYS * 24 * 3600, "httponly": True, "samesite": "Lax",
        "secure": settings.SESSION_COOKIE_SECURE, "path": "/"})]


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
    # Wrong codes per account per day, across sign-ins: someone with the password can't keep guessing.
    # A recovery code (10 characters, can't be guessed) still works, so the owner is never locked out.
    six_digits = len(totp.clean(code)) == totp.DIGITS
    wait = throttle.place_wait(["totp_day"], user.username) if six_digits else 0
    if wait:
        hours = max(1, round(wait / 3600))
        raise ApiError("too_many_attempts", _("Too many wrong codes today. Try again in %(hours)s hours, or enter a recovery code.") % {"hours": hours},  # noqa: E501
                       429, retry_after=wait)
    if _accept_code(user, code):
        _complete(request, user, mfa=True)
        user.refresh_from_db(fields=["password", "totp_secret", "totp_confirmed_at"])
        _remember_device(request, user)
        return {"next": next_step(request, user)}
    if _code_just_used(user, code):  # right, but already used: no strike, and a message that says so
        raise ApiError("code_used", _("This code was just used. Wait for the next code, then enter it."), 400)
    # Every refused code is a strike, the clock-off one too (it is still a guess that didn't get in).
    failures = request.session.get(PENDING_FAILURES, 0) + 1
    request.session[PENDING_FAILURES] = failures
    event(user.id, "totp_failed", request)
    if not throttle.place_take("totp_day", user.username):
        event(user.id, "totp_locked", request)
    if failures >= throttle.LIMITS["totp"]:
        for key in (PENDING_USER, PENDING_UNTIL, PENDING_FAILURES):
            request.session.pop(key, None)
        throttle.record_failure([("user", user.username)])
        raise ApiError("signed_out", _("Too many wrong codes. Sign in again."), 401)
    _clock_off(user.totp_secret, code)
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


def _clock_off(secret, code):
    """A code that is right for another time: the phone's clock is wrong. Said plainly, never accepted."""
    minutes = totp.clock_off_minutes(crypto.decrypt(secret), code)
    if minutes:
        message = _("Your phone's clock is about %(minutes)s minutes off. In the phone's settings, turn on automatic date and time, then enter the new code.")  # noqa: E501
        raise ApiError("clock_off", message % {"minutes": minutes}, 400)


def _code_just_used(user, code):
    if len(totp.clean(code)) != totp.DIGITS:
        return False
    user.refresh_from_db(fields=["totp_last_step"])
    return totp.used_recently(crypto.decrypt(user.totp_secret), code, last_step=user.totp_last_step)


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
    # The laptop copy has its own name in the authenticator app, so its codes are never taken for the live site's.
    uri = totp.provisioning_uri(secret, user.username, issuer="Dento laptop" if settings.DEBUG else "Dento")
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
        _clock_off(user.totp_secret, code)
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
