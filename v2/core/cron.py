"""Daily upkeep on hosting with no worker (Instinct review, 2026-11-21).

Vercel runs no Procrastinate worker, so the periodic tasks in core/jobs.py and scheduling/jobs.py never ran
on the live site: days were not closed, summaries not recomputed, request keys never cleaned. Vercel Cron
calls GET /cron/daily once a day with "Authorization: Bearer <CRON_SECRET>"; this view runs the same work.
Each step runs on its own, so one failing step does not stop the others. Without CRON_SECRET the URL is off.
"""

import hmac
import logging
import os

from django.http import HttpResponseNotFound, JsonResponse
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)


def _allowed(request):
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        return False
    sent = request.headers.get("Authorization", "")
    return hmac.compare_digest(sent.encode(), f"Bearer {secret}".encode())


def run_daily():
    from core.jobs import clean_request_keys
    from core.partitions import ensure_all
    from scheduling.jobs import run_recompute, run_sweep

    steps = [
        ("partitions", lambda: {alias: list(tables) for alias, tables in ensure_all().items()}),
        ("sweep", lambda: run_sweep()),
        ("summaries", lambda: len(run_recompute())),
        ("request_keys", lambda: clean_request_keys()),
    ]
    out = {}
    for name, step in steps:
        try:
            out[name] = {"ok": True, "result": step()}
        except Exception as error:  # one step failing must not stop the rest
            log.exception("daily upkeep step failed: %s", name)
            out[name] = {"ok": False, "error": type(error).__name__}
    return out


@require_GET
def daily(request):
    if not _allowed(request):
        return HttpResponseNotFound()
    result = run_daily()
    status = 200 if all(step["ok"] for step in result.values()) else 500
    return JsonResponse(result, status=status, json_dumps_params={"default": str})
