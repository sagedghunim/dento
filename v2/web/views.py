"""Server-rendered pages (design 7.1, 7.3). Actions go through /api/v1/ from api.js."""

from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from accounts.services import PENDING_UNTIL, PENDING_USER
from core.api import SESSION_ACTIVE, page
from directory.services import clinics_for_user


def _safe_next(request, default="/"):
    target = request.GET.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return default


@require_GET
@ensure_csrf_cookie
def login_page(request):
    pending = bool(request.session.get(PENDING_USER))
    if request.user.is_authenticated and not pending and request.GET.get("step") != "code":
        return redirect(_safe_next(request))
    return render(request, "web/login.html", {"step": "code" if pending else "password", "next": _safe_next(request)})


@require_GET
@ensure_csrf_cookie
@page(member=False)
def clinics_page(request, ctx):
    from directory.services import may_add_place

    return render(request, "web/clinics.html", {"clinics": clinics_for_user(request.user.id),
                                                "active": request.session.get(SESSION_ACTIVE),
                                                "may_add_place": may_add_place(request.user)})


@require_GET
@ensure_csrf_cookie
@page(member=False, allow_password_change=True)
def password_page(request, ctx):
    return render(request, "web/password.html", {"forced": request.user.must_change_password})


@require_GET
@ensure_csrf_cookie
@page(member=False)
def security_page(request, ctx):
    return render(request, "web/security.html", {"has_totp": request.user.has_totp})


@require_GET
def home(request):
    if request.user.is_authenticated and request.user.is_platform_admin:
        return redirect("/platform")
    from .pages import today_page

    return today_page(request)  # Today is the home page for the desk and the doctor (design 7.3)
