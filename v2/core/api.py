"""Request plumbing for JSON actions and pages (design 5.0, 6.1).

A clinic request runs as one transaction on the clinic's shard:
  1. method, session, forced password change, two-factor state, X-Membership;
  2. BEGIN on the shard; set_config('app.clinic_id', ...) for row-level security;
  3. load the membership and the clinic (a clinic the shard doesn't own is 503);
  4. claim the Idempotency-Key for a mutating request (a repeat replays);
  5. check the declared permission, build the Ctx, call the view.
Any answer other than 2xx rolls back, the key claim included. A deadlock is
retried once; a lock timeout is 409 `busy` with Retry-After.
"""

import functools
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

from django.conf import settings
from django.db import OperationalError, connections, transaction
from django.http import HttpResponseNotModified, JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from psycopg import errors as pg_errors

from core.context import Ctx
from core.errors import ApiError
from core.routing import clinic_transaction

log = logging.getLogger(__name__)

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SESSION_ACTIVE = "hamada_active"  # {"clinic_id": ..., "membership_id": ...}
SESSION_MFA = "hamada_mfa_verified"


@dataclass
class Result:
    body: dict
    status: int = 200
    target_type: str = ""
    target_id: int | None = None
    headers: dict = field(default_factory=dict)  # e.g. ETag and Poll-After (design 5.11)


def client_ip(request):
    header = settings.CLIENT_IP_HEADER
    if header:
        value = request.META.get(header, "")
        if value:
            return value.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def _json_body(request):
    from django.core.exceptions import RequestDataTooBig

    try:
        body = request.body
    except RequestDataTooBig:  # a clear answer, not Django's HTML page (Saged, 2026-11-23)
        raise ApiError("too_big", _("That is too long to save. Shorten it."), 413)
    if not body:
        return {}
    try:
        data = json.loads(request.body)
    except ValueError:
        raise ApiError("invalid_body", _("The request couldn't be read."))
    if not isinstance(data, dict):
        raise ApiError("invalid_body", _("The request couldn't be read."))
    return data


UPLOAD_OVERHEAD = 64 * 1024  # multipart boundaries and the small text fields beside the file


def _upload_body(request, max_bytes):
    """A multipart upload (design E3 attachments): the text fields, and the file's size and SHA-256 for the hash.

    The declared length is checked before anything is read, so an oversized body never reaches the disk.
    The view reads the file itself from request.FILES["file"].
    """
    if not request.content_type.startswith("multipart/form-data"):
        raise ApiError("invalid_body", _("Send the file as a form upload."), 415)
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > max_bytes + UPLOAD_OVERHEAD:
        raise ApiError("too_large", _("The file is larger than 10 MB."), 413, fields={"file": "too_large"})
    data = {name: request.POST.get(name) for name in request.POST}
    upload = request.FILES.get("file")
    if upload is not None:
        digest = hashlib.sha256()
        for chunk in upload.chunks():
            digest.update(chunk)
        upload.seek(0)
        data["file"] = {"size": upload.size, "sha256": digest.hexdigest()}
    return data


def request_hash(request, data):
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(f"{request.method} {request.path}\n{canonical}".encode()).hexdigest()


def _request_key(request):
    raw = request.headers.get("Idempotency-Key", "")
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ApiError("key_required", _("This request needs an Idempotency-Key."), 400)


def _respond(request, result, key=None):
    if isinstance(result, HttpResponseBase):
        return result  # already answered, e.g. by keyed_public_call
    if not isinstance(result, Result):
        result = Result(result or {})
    if result.status == 304:
        response = HttpResponseNotModified()
    else:
        response = JsonResponse(result.body, status=result.status, json_dumps_params={"ensure_ascii": False})
    for name, value in result.headers.items():
        response[name] = value
    if key is not None:
        response["Idempotency-Key"] = str(key)
    for name, value, options in getattr(request, "hamada_cookies", ()):  # e.g. the remembered device (accounts)
        response.set_cookie(name, value, **options)
    return response


def _error_response(request, error):
    response = JsonResponse(error.body(), status=error.status, json_dumps_params={"ensure_ascii": False})
    if error.retry_after:
        response["Retry-After"] = str(error.retry_after)
    return response


def _require_user(request, allow_password_change):
    user = request.user
    if not user.is_authenticated:
        raise ApiError("signed_out", _("Please sign in."), 401)
    if user.must_change_password and not allow_password_change:
        raise ApiError("password_change_required", _("Change your password to continue."), 403)
    return user


def _active_scope(request):
    from directory.services import shard_for

    active = request.session.get(SESSION_ACTIVE)
    if not active:
        raise ApiError("no_clinic", _("Pick a clinic first."), 403)
    alias = shard_for(active["clinic_id"])
    if alias is None:
        raise ApiError("no_clinic", _("Pick a clinic first."), 403)
    return active["clinic_id"], active["membership_id"], alias


def mfa_required(user, membership, clinic):
    return membership.role == "owner" or user.is_platform_admin or bool(clinic.settings.get("require_mfa"))


def build_ctx(request, user, alias, clinic_id, membership_id):
    """Load the membership and its clinic on the shard (inside clinic_transaction) and check them."""
    from clinics.models import Clinic, Membership
    from clinics.permissions import effective_codes
    from directory.services import forget

    membership = (Membership.objects.select_related("clinic")
                  .filter(clinic_id=clinic_id, id=membership_id, user_id=user.id).first())
    clinic = membership.clinic if membership else Clinic.objects.filter(id=clinic_id).first()
    if clinic is None or not clinic.owned:
        forget(clinic_id)
        raise ApiError("clinic_moved", _("The clinic is being moved. Try again in a moment."), 503, retry_after=2)
    if clinic.status != Clinic.Status.ACTIVE:
        raise ApiError("clinic_suspended", _("This clinic is suspended."), 403)
    if membership is None or not membership.is_active:
        request.session.pop(SESSION_ACTIVE, None)
        raise ApiError("membership_inactive", _("You are no longer a member of this clinic."), 403)
    if mfa_required(user, membership, clinic):
        if not user.has_totp:
            raise ApiError("mfa_setup_required", _("Set up two-step sign-in to continue."), 403)
        if not request.session.get(SESSION_MFA):
            raise ApiError("mfa_required", _("Enter the code from your authenticator app."), 403)
    return Ctx(
        clinic=clinic, membership=membership, user_id=user.id, actor="member",
        codes=effective_codes(membership.role, membership.granted, membership.revoked),
        mfa_verified=bool(request.session.get(SESSION_MFA)), request_id=getattr(request, "request_id", ""),
        ip=client_ip(request), alias=alias,
    )


def _claim(alias, table, scope_column, scope_id, actor_column, actor_id, key, endpoint, digest):
    """Claim a key; return None when this request owns it, or the stored row for a repeat (design 5.0)."""
    with connections[alias].cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} ({scope_column}, key, {actor_column}endpoint, request_hash, status, target_type, created_at) "
            f"VALUES (%s, %s, {'%s, ' if actor_column else ''}%s, %s, 0, '', now()) ON CONFLICT DO NOTHING RETURNING 1",
            [scope_id, key, *([actor_id] if actor_column else []), endpoint, digest],
        )
        if cursor.fetchone():
            return None
        cursor.execute(
            f"SELECT {actor_column}endpoint, request_hash, status, target_type, target_id FROM {table} "
            f"WHERE {scope_column} = %s AND key = %s",
            [scope_id, key],
        )
        row = cursor.fetchone()
    stored_actor, (stored_endpoint, stored_hash, status, target_type, target_id) = (
        (row[0], row[1:]) if actor_column else (None, row)
    )
    if stored_endpoint != endpoint or stored_hash != digest or (actor_column and stored_actor != actor_id):
        raise ApiError("request_key_reused", _("This request key was already used for a different request."), 422)
    return {"status": status, "target_type": target_type, "target_id": target_id}


def _record(alias, table, scope_column, scope_id, key, result):
    with connections[alias].cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET status = %s, target_type = %s, target_id = %s WHERE {scope_column} = %s AND key = %s",
            [result.status, result.target_type, result.target_id, scope_id, key],
        )


def _replayed(ctx_or_user, stored, replay):
    if stored.get("target_type") == "cash_request":  # it waited for the owner's permission (Saged, 2026-11-25)
        from finance.views import _replay_request

        replay = _replay_request
    if replay is not None:
        result = replay(ctx_or_user, stored)
        if not isinstance(result, Result):
            result = Result(result)
        result.status = stored["status"] or result.status
        return result
    return Result({"replayed": True, "target": {"type": stored["target_type"], "id": stored["target_id"]}},
                  status=stored["status"] or 200)


def _db_error(error):
    cause = error.__cause__
    if isinstance(cause, pg_errors.LockNotAvailable):
        return ApiError("busy", _("Someone else is changing this. Trying again."), 409, retry_after=2)
    if isinstance(cause, pg_errors.QueryCanceled):
        return ApiError("timeout", _("This took too long. Try again."), 503, retry_after=2)
    return None


def api(method="GET", perm=None, access=None, member=True, key=True, allow_password_change=False, replay=None,
        headers=None, upload=None):
    """Declare a JSON endpoint: its method, permission codes and whether it returns clinical data.

    member=True views are called as view(request, ctx, data, ...); member=False views
    (accounts, the clinic picker) as view(request, data, ...). `access` names the
    access-log action the view records (a test checks every clinical view declares it).
    `headers` is a callable returning headers for every answer, refusals included (Today's
    Poll-After, design 5.11); a successful answer's own headers take precedence.
    `upload` (a size in bytes) makes a mutating view take a multipart body with one `file` (design E3).
    """
    codes = (perm,) if isinstance(perm, str) else tuple(perm or ())
    needs_key = key and method in MUTATING

    def wrap(view):
        endpoint = f"{view.__module__.rsplit('.', 2)[-2]}.{view.__name__}"

        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            request.request_id = str(uuid.uuid4())
            try:
                if request.method != method:
                    raise ApiError("method_not_allowed", _("This method isn't allowed here."), 405)
                if method not in MUTATING:
                    data = {}
                elif upload:
                    _require_user(request, allow_password_change)  # nobody signed out gets a file read
                    data = _upload_body(request, upload)
                else:
                    data = _json_body(request)
                if not member:
                    response = _account_call(request, view, data, args, kwargs, endpoint, needs_key,
                                             allow_password_change, replay)
                else:
                    user = _require_user(request, allow_password_change)
                    clinic_id, membership_id, alias = _active_scope(request)
                    if request.headers.get("X-Membership") != str(membership_id):
                        raise ApiError("clinic_changed", _("Another tab switched clinic. Reloading."), 409)
                    response = _clinic_call(request, user, alias, clinic_id, membership_id, view, data, args, kwargs,
                                            codes, endpoint, needs_key, replay)
            except ApiError as error:
                response = _error_response(request, error)
            if headers is not None:
                for name, value in headers().items():
                    if name not in response:
                        response[name] = value
            return response

        inner.api_meta = {"method": method, "perm": codes, "access": access, "member": member, "key": needs_key}
        return inner

    return wrap


def _clinic_call(request, user, alias, clinic_id, membership_id, view, data, args, kwargs, codes, endpoint, needs_key, replay):
    key = _request_key(request) if needs_key else None
    for attempt in (0, 1):
        try:
            with clinic_transaction(clinic_id, alias):
                ctx = build_ctx(request, user, alias, clinic_id, membership_id)
                if codes:
                    ctx.require(*codes)
                if key is not None:
                    stored = _claim(alias, "core_request_key", "clinic_id", clinic_id, "membership_id, ",
                                    membership_id, key, endpoint, request_hash(request, data))
                    if stored is not None:
                        return _respond(request, _replayed(ctx, stored, replay), key)
                result = view(request, ctx, data, *args, **kwargs)
                if isinstance(result, HttpResponseBase) and key is None:
                    return result  # a file a GET streams (an attachment, design E3)
                if not isinstance(result, Result):
                    result = Result(result or {})
                if key is not None:
                    _record(alias, "core_request_key", "clinic_id", clinic_id, key, result)
            return _respond(request, result, key)
        except OperationalError as error:
            if isinstance(error.__cause__, pg_errors.DeadlockDetected) and attempt == 0:
                log.warning("Deadlock on %s, retrying once", endpoint)
                continue
            mapped = _db_error(error)
            if mapped is None:
                raise
            raise mapped


def _account_call(request, view, data, args, kwargs, endpoint, needs_key, allow_password_change, replay):
    """Directory endpoints: sign-in, the picker, account settings, the platform admin."""
    user = request.user if request.user.is_authenticated else None
    if needs_key:
        user = _require_user(request, allow_password_change)
        key = _request_key(request)
        with transaction.atomic(using="default"):
            stored = _claim("default", "accounts_request_key", "user_id", user.id, "", None, key, endpoint,
                            request_hash(request, data))
            if stored is not None:
                return _respond(request, _replayed(user, stored, replay), key)
            result = view(request, data, *args, **kwargs)
            if not isinstance(result, Result):
                result = Result(result or {})
            _record("default", "accounts_request_key", "user_id", user.id, key, result)
        return _respond(request, result, key)
    return _respond(request, view(request, data, *args, **kwargs))


# Request keys of public endpoints (sign-up) belong to no account yet; random UUIDs keep them apart.
PUBLIC_KEY_SCOPE = 0


def keyed_public_call(request, endpoint, data, work, replay, hash_data=None):
    """A public mutating action with a request key (design D2): claim, work and record in one directory transaction.

    `work()` returns a Result naming its target; `replay(request, stored)` rebuilds the first answer.
    `hash_data` replaces `data` in the request hash (sign-up keys a password's HMAC, never the password).
    Any refusal rolls back the claim with the work, so the same key may run again.
    """
    key = _request_key(request)
    digest = request_hash(request, data if hash_data is None else hash_data)
    with transaction.atomic(using="default"):
        stored = _claim("default", "accounts_request_key", "user_id", PUBLIC_KEY_SCOPE, "", None, key, endpoint, digest)
        if stored is not None:
            result = _replayed(request, stored, replay)
        else:
            result = work()
            _record("default", "accounts_request_key", "user_id", PUBLIC_KEY_SCOPE, key, result)
    return _respond(request, result, key)


def page(perm=None, member=True, allow_password_change=False, template_403="web/forbidden.html"):
    """Declare an HTML page. Clinic pages render inside the request's shard transaction."""
    codes = (perm,) if isinstance(perm, str) else tuple(perm or ())

    def wrap(view):
        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            request.request_id = str(uuid.uuid4())
            user = request.user
            if not user.is_authenticated:
                return redirect(f"/login?next={quote(request.get_full_path())}")
            if user.must_change_password and not allow_password_change:
                return redirect("/account/password")
            if not member:
                return view(request, None, *args, **kwargs)
            try:
                clinic_id, membership_id, alias = _active_scope(request)
                with clinic_transaction(clinic_id, alias):
                    ctx = build_ctx(request, user, alias, clinic_id, membership_id)
                    if codes and not all(ctx.can(code) for code in codes):
                        return render(request, template_403, {"ctx": ctx}, status=403)
                    return view(request, ctx, *args, **kwargs)
            except ApiError as error:
                if error.code == "mfa_setup_required":
                    return redirect("/account/security")
                if error.code == "mfa_required":
                    return redirect("/login?step=code")
                if error.code in ("no_clinic", "membership_inactive", "clinic_suspended"):
                    return redirect("/clinics")
                if error.code == "clinic_moved":
                    response = render(request, "web/retry.html", status=503)
                    response["Retry-After"] = "2"
                    return response
                raise

        inner.page_meta = {"perm": codes, "member": member}
        return inner

    return wrap


def by_method(**views):
    """One URL, several methods: by_method(GET=search_view, POST=create_view).

    Each view keeps its own @api declaration; `api_views` lets tests read them from the URL configuration.
    """

    def dispatch(request, *args, **kwargs):
        view = views.get(request.method)
        if view is None:
            return _error_response(request, ApiError("method_not_allowed", _("This method isn't allowed here."), 405))
        return view(request, *args, **kwargs)

    dispatch.api_views = views
    return dispatch
