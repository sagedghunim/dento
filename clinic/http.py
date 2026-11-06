"""Request plumbing: who is calling, what they may do, and safe JSON responses."""
import functools
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import JsonResponse

from .models import IdempotencyKey, Membership, Role, RolePermission

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


class ClinicMiddleware:
    """Tenant comes from the signed-in user on the server, never from the browser."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.membership = None
        if request.user.is_authenticated:
            request.membership = (
                Membership.objects.select_related("clinic", "clinic__specialty")
                .filter(user=request.user).order_by("id").first()
            )
        return self.get_response(request)


def perms_for(membership):
    from .models import Perm
    if membership.role == Role.OWNER:
        return set(Perm.values)
    return set(
        RolePermission.objects.filter(clinic_id=membership.clinic_id, role=membership.role, allowed=True)
        .values_list("perm", flat=True)
    )


def client_ip(request):
    header = settings.CLIENT_IP_HEADER
    if header:
        value = request.META.get(header, "")
        if value:
            return value.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def rate_limit(key, limit, seconds):
    """Returns False once `limit` hits land inside the window."""
    cache.add(key, 0, seconds)
    try:
        return cache.incr(key) <= limit
    except ValueError:
        return True


def api(method="GET", perm=None, public=False):
    def wrap(view):
        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            if request.method != method:
                return JsonResponse({"error": "Method not allowed"}, status=405)
            try:
                if not public:
                    if not request.user.is_authenticated:
                        raise ApiError("Please sign in", 401)
                    if request.membership is None:
                        raise ApiError("This account has no clinic", 403)
                    request.perms = perms_for(request.membership)
                    if perm and perm not in request.perms:
                        raise ApiError("You do not have permission to do this", 403)
                request.data = {}
                if method == "POST" and request.body:
                    try:
                        request.data = json.loads(request.body)
                    except ValueError:
                        raise ApiError("Invalid request body")
                    if not isinstance(request.data, dict):
                        raise ApiError("Invalid request body")
                if method == "POST" and not public:
                    return _idempotent(view, request, args, kwargs)
                return JsonResponse(view(request, *args, **kwargs) or {})
            except ApiError as e:
                return JsonResponse({"error": e.message}, status=e.status)
        return inner
    return wrap


def _idempotent(view, request, args, kwargs):
    key = request.headers.get("Idempotency-Key", "")[:64]
    if not key:
        with transaction.atomic():
            return JsonResponse(view(request, *args, **kwargs) or {})
    clinic_id = request.membership.clinic_id
    try:
        with transaction.atomic():
            IdempotencyKey.objects.create(clinic_id=clinic_id, key=key)
            body = view(request, *args, **kwargs) or {}
            IdempotencyKey.objects.filter(clinic_id=clinic_id, key=key).update(status=200, body=body)
        return JsonResponse(body)
    except IntegrityError:
        row = IdempotencyKey.objects.filter(clinic_id=clinic_id, key=key).first()
        if row is None or row.status == 0:
            raise ApiError("This request is already being processed", 409)
        return JsonResponse(row.body)


def text(data, name, max_len, required=True):
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ApiError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ApiError(f"{name} is required")
    if len(value) > max_len:
        raise ApiError(f"{name} is too long")
    return value


def integer(data, name, low, high):
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(f"{name} must be a whole number")
    if not low <= value <= high:
        raise ApiError(f"{name} must be between {low} and {high}")
    return value
