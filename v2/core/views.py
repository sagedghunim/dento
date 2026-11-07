from django.conf import settings
from django.db import connections
from django.http import HttpResponse, JsonResponse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET


@require_GET
def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


@require_GET
def readyz(request):
    """The directory and every shard answer."""
    for alias in ["default", *settings.SHARDS]:
        with connections[alias].cursor() as cursor:
            cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


def csrf_failure(request, reason=""):
    """JSON, so the client can fetch a fresh token once and resend (design 6.1)."""
    return JsonResponse({"error": {"code": "csrf_failed", "message": _("Your session changed. Trying again.")}},
                        status=403)
