"""HTML fragments fetched by the page's scripts (design 5.11, 7.1).

A fragment is checked like a JSON GET under @api: the session, the forced password change, the
two-factor state, X-Membership against another tab's clinic, then one transaction on the clinic's
shard with row-level security. Its answer is HTML; its errors are the API's JSON errors, so the
scripts handle a 401, a 409 `clinic_changed` or a 503 the same way everywhere.
"""

import functools
import uuid

from django.db import OperationalError
from django.utils.translation import gettext as _

from core.api import _active_scope, _db_error, _error_response, _require_user, build_ctx
from core.errors import ApiError
from core.routing import clinic_transaction

from . import format as fmt


def fragment(perm=None, any_of=None):
    """`perm`: codes all required; `any_of`: at least one of these codes (Today takes book or queue)."""
    codes = (perm,) if isinstance(perm, str) else tuple(perm or ())

    def wrap(view):
        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            request.request_id = str(uuid.uuid4())
            try:
                if request.method != "GET":
                    raise ApiError("method_not_allowed", _("This method isn't allowed here."), 405)
                user = _require_user(request, allow_password_change=False)
                clinic_id, membership_id, alias = _active_scope(request)
                if request.headers.get("X-Membership") != str(membership_id):
                    raise ApiError("clinic_changed", _("Another tab switched clinic. Reloading."), 409)
                try:
                    with clinic_transaction(clinic_id, alias):
                        ctx = build_ctx(request, user, alias, clinic_id, membership_id)
                        if codes:
                            ctx.require(*codes)
                        if any_of and not any(ctx.can(code) for code in any_of):
                            ctx.require(any_of[0])
                        with fmt.using(ctx.clinic):
                            return view(request, ctx, *args, **kwargs)
                except OperationalError as error:
                    mapped = _db_error(error)
                    if mapped is None:
                        raise
                    raise mapped
            except ApiError as error:
                return _error_response(request, error)

        inner.fragment_meta = {"perm": codes, "any_of": tuple(any_of or ())}
        return inner

    return wrap


def clinic_page(view):
    """Inside core.api.page(): render with the clinic's digits, reset afterwards."""

    @functools.wraps(view)
    def inner(request, ctx, *args, **kwargs):
        with fmt.using(ctx.clinic):
            return view(request, ctx, *args, **kwargs)

    return inner


def route_exists(path):
    """True when the URL configuration has this path: screens offer an action only once its endpoint exists."""
    from django.urls import Resolver404, resolve

    try:
        resolve(path)
    except Resolver404:
        return False
    return True
