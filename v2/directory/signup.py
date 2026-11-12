"""Self-service sign-up and adding a place (design D2, requirements 4.10).

A new owner signs up from the sign-in page; a signed-in account adds another place from the picker.
Either way the place is created by `flows.create_clinic` on the least full shard, with one section
for a clinic or 2-20 for a center, and a first queue session for the owner so it can book at once.

Abuse controls: Django's password validators, a hidden honeypot field (`website`), throttles in
PostgreSQL per client IP (3 new places an hour and 10 a day; 20 attempts an hour, which bounds how
often a taken username or mobile can be probed), and an account event with the IP.
"""

import copy
import hashlib
import hmac
import re

from django.conf import settings
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _

from accounts import throttle
from accounts.models import User
from accounts.services import _complete, event, pick_clinic, sign_in
from core.api import Result, client_ip, keyed_public_call
from core.errors import ApiError
from core.text import clean_name, is_valid_phone, normalize_phone
from core.validate import text

from .flows import create_account, create_planned, plan_places
from .models import DirectoryClinic, UserClinic

HONEYPOT = "website"
USERNAME = re.compile(r"^[a-z][a-z0-9._-]{2,29}$")
SIGNUP_ENDPOINT = "directory.signup_view"


def _nested(prefix, call):
    """Run a field validator on a nested object and name its fields "<prefix>.<field>"."""
    try:
        return call()
    except ApiError as error:
        error.fields = {f"{prefix}.{name}": code for name, code in error.fields.items()}
        raise


def clean_place(data, language):
    """kind, sections, layout, name, city and an optional phone, validated as the platform admin's form is (D2, E2).

    `sections` is [{specialty, count?, name?}]: a card with a count (names made from the specialty), or one entry
    per section with count 1 and its own name. `layout` is "one_place" (default) or "branches"; with branches each
    specialty becomes its own place under the same owner. Returns the places to create, each a list of sections.
    """
    layout = data.get("layout") or "one_place"
    places = plan_places(data.get("kind"), data.get("sections"), language, layout)
    name = clean_name(text(data, "name", 120))
    city = clean_name(text(data, "city", 60))
    phone = normalize_phone(text(data, "phone", 20, required=False))
    if phone and not is_valid_phone(phone):
        raise ApiError("invalid", _("Enter a valid phone number."), fields={"phone": "phone"})
    return {"kind": data["kind"], "layout": layout, "places": places, "name": name, "city": city, "phone": phone}


def clean_owner(data):
    owner = data.get("owner")
    if not isinstance(owner, dict):
        raise ApiError("invalid", "owner must be an object", fields={"owner": "object"})
    full_name = clean_name(_nested("owner", lambda: text(owner, "full_name", 120)))
    mobile = normalize_phone(_nested("owner", lambda: text(owner, "mobile", 20)))
    if not is_valid_phone(mobile):
        raise ApiError("invalid", _("Enter a valid mobile number."), fields={"owner.mobile": "phone"})
    username = _nested("owner", lambda: text(owner, "username", 150)).lower()
    if not USERNAME.match(username):
        message = _("A username has 3 to 30 English letters, digits, dots, dashes or underscores, and starts with a letter.")
        raise ApiError("invalid", message, fields={"owner.username": "format"})
    password = _nested("owner", lambda: text(owner, "password", 200, strip=False))
    try:
        password_validation.validate_password(password, User(username=username, full_name=full_name, phone=mobile))
    except ValidationError as error:
        raise ApiError("weak_password", " ".join(error.messages), 400, fields={"owner.password": "weak"})
    return {"full_name": full_name, "mobile": mobile, "username": username, "password": password}


def _too_many(wait):
    minutes = max(1, round(wait / 60))
    return ApiError("too_many_signups",
                    _("Too many new places from this connection. Try again in %(minutes)s minutes.") % {"minutes": minutes},
                    429, retry_after=wait)


def _take_place(ip):
    """Count a new place against the per-IP limits, inside the creating transaction (a refusal gives it back)."""
    for scope in ("signup_h", "signup_d"):
        if not throttle.place_take(scope, ip):
            raise _too_many(throttle.place_wait(("signup_h", "signup_d"), ip) or 60)


def _refuse_taken(username, mobile):
    fields = {}
    if User.objects.filter(username=username).exists():
        fields["owner.username"] = "taken"
    if User.objects.filter(phone=mobile).exists():
        fields["owner.mobile"] = "taken"
    if "owner.username" in fields:
        raise ApiError("username_taken", _("That username is taken."), 409, fields=fields)
    if fields:
        raise ApiError("phone_taken", _("That mobile number already has an account."), 409, fields=fields)


def _hash_data(data):
    """The body as the request key hashes it: the password replaced by its HMAC with the server's key.

    The request-key table keeps the hash for 72 hours; a plain SHA-256 of a body holding a password
    would be a fast, unsalted password hash.
    """
    hashed = copy.deepcopy(data)
    owner = hashed.get("owner")
    if isinstance(owner, dict) and isinstance(owner.get("password"), str):
        owner["password"] = hmac.new(settings.SECRET_KEY.encode(), owner["password"].encode(), hashlib.sha256).hexdigest()
    return hashed


def _membership_id(user, entry_id):
    return UserClinic.objects.filter(user=user, clinic_id=entry_id).values_list("membership_id", flat=True).first()


def sign_up(request, data):
    """POST /api/v1/signup (public, CSRF, request key). Answers 201 {"next": "security"} and signs the owner in."""
    if data.get(HONEYPOT):
        # Only a bot fills the hidden field: answer as if it worked, create nothing.
        throttle.place_take("signup_try", client_ip(request) or "-")
        event(None, "signup_honeypot", request)
        return Result({"next": "security"}, status=201)
    language = getattr(request, "LANGUAGE_CODE", "ar")
    place = clean_place(data, language)
    owner = clean_owner(data)
    ip = client_ip(request) or "-"
    wait = throttle.place_wait(("signup_h", "signup_d", "signup_try"), ip)
    if wait:
        raise _too_many(wait)
    throttle.place_take("signup_try", ip)  # every attempt that got this far, committed whatever happens next

    def work():
        _take_place(ip)
        _refuse_taken(owner["username"], owner["mobile"])
        try:
            with transaction.atomic(using="default"):
                user = create_account(owner["username"], owner["full_name"], phone=owner["mobile"],
                                      password=owner["password"], language=language)
        except IntegrityError:
            # Another sign-up took the username or mobile between the check and the insert.
            _refuse_taken(owner["username"], owner["mobile"])
            raise
        entry = create_planned(name=place["name"], owner=user, kind=place["kind"], places=place["places"],
                               city=place["city"], phone=place["phone"], language=language, default_session=True)[0]
        event(user.id, "signup", request)
        _complete(request, user, mfa=False)
        pick_clinic(request, user, _membership_id(user, entry.id))
        return Result({"next": "security"}, status=201, target_type="user", target_id=user.id)

    def replay(request, stored):
        # The same key and body, password included: sign this browser in as the first answer did. The normal
        # sign-in path checks the password again and asks for the code once two-step sign-in is set up.
        answer = sign_in(request, owner["username"], owner["password"])
        if answer["next"] == "code":
            return answer
        return {"next": "security"} if not request.user.has_totp else answer

    return keyed_public_call(request, SIGNUP_ENDPOINT, data, work, replay, hash_data=_hash_data(data))


def place_body(user, entry, entries=None):
    """The place that became active, and every place the request created (several for separate branches, E2)."""
    membership_id = _membership_id(user, entry.id)
    return {"clinic": {"id": entry.id, "name": entry.name, "kind": entry.kind}, "membership_id": membership_id,
            "clinics": [{"id": e.id, "name": e.name, "kind": e.kind} for e in (entries or [entry])],
            "next": "home" if user.has_totp else "security"}


def add_place(request, user, data):
    """POST /api/v1/me/places: the signed-in account adds a place and becomes its owner (D2).

    The same body as sign-up without `owner`. The new place becomes the session's active one.
    """
    if user.is_platform_admin:
        raise ApiError("forbidden", _("You don't have permission to do this."), 403)
    place = clean_place(data, user.language)
    ip = client_ip(request) or "-"
    wait = throttle.place_wait(("signup_h", "signup_d"), ip)
    if wait:
        raise _too_many(wait)
    _take_place(ip)
    entries = create_planned(name=place["name"], owner=user, kind=place["kind"], places=place["places"],
                             city=place["city"], phone=place["phone"], language=user.language, default_session=True)
    entry = entries[0]
    event(user.id, "place_added", request)
    pick_clinic(request, user, _membership_id(user, entry.id))
    return Result(place_body(user, entry, entries), status=201, target_type="clinic", target_id=entry.id)


def replay_place(user, stored):
    entry = DirectoryClinic.objects.filter(id=stored["target_id"]).first()
    if entry is None:
        return {"clinic": None, "membership_id": None, "next": "clinics"}
    return place_body(user, entry)
