"""Thin HTTP layer. Reads are scoped to request.membership.clinic; writes go through services."""
from django.contrib.auth import authenticate, login, logout
from django.db import connection
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from . import services
from .http import ApiError, api, client_ip, integer, rate_limit, text
from .models import Consultation, Perm, Role, RolePermission, Specialty, StockMovement

PAGE = 100
MAX_PRICE = services.MAX_MONEY


@ensure_csrf_cookie
@require_GET
def app(request):
    return render(request, "app.html")


@require_GET
def healthz(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


@api("POST", public=True)
def api_login(request):
    username = text(request.data, "username", 150)
    password = text(request.data, "password", 200)
    ip = client_ip(request)
    allowed = rate_limit(f"login:ip:{ip}", 30, 300) & rate_limit(f"login:user:{username.lower()}", 8, 300)
    if not allowed:
        raise ApiError("Too many attempts. Wait a few minutes and try again.", 429)
    user = authenticate(request, username=username, password=password)
    if user is None:
        raise ApiError("Wrong username or password", 401)
    login(request, user)
    return {"ok": True}


@api("POST")
def api_logout(request):
    logout(request)
    return {"ok": True}


@api()
def me(request):
    m, c = request.membership, request.membership.clinic
    return {"name": m.display_name, "role": m.role, "perms": sorted(request.perms),
            "clinic": {"name": c.name, "city": c.city, "specialty": c.specialty.name, "services": c.specialty.services},
            "today": services.local_now(c).strftime("%A, %d %B %Y")}


def stock_payload(clinic):
    return [{"id": i.id, "name": i.name, "unit": i.unit, "pack": i.pack, "quantity": i.quantity,
             "threshold": i.threshold, "price": i.unit_price} for i in clinic.stock_items.order_by("id")]


@api()
def today(request):
    clinic = request.membership.clinic
    start, end = services.day_bounds(clinic)
    zone = services.tz(clinic)
    rows = clinic.appointments.filter(starts_at__gte=start, starts_at__lt=end).select_related("patient", "doctor").order_by("starts_at", "id")[:300]
    feed = clinic.activity.filter(sensitive=False).order_by("-created_at", "-id")[:6]
    return {
        "appointments": [{"id": a.id, "patient": a.patient.name, "service": a.service,
                          "doctor": a.doctor.display_name if a.doctor else "",
                          "time": a.starts_at.astimezone(zone).strftime("%H:%M"), "status": a.status, "isNew": a.is_new} for a in rows],
        "feed": [{"when": f.created_at.astimezone(zone).strftime("%H:%M"), "text": f.text} for f in feed],
        "stock": stock_payload(clinic),
    }


@api("POST", perm=Perm.BOOKING)
def book(request):
    d = request.data
    services.book(request.membership, text(d, "name", 120), text(d, "service", 120), text(d, "time", 5),
                  text(d, "source", 10), integer(d, "fee", 0, MAX_PRICE))
    return {"message": "Booking added — it is already on today's list"}


@api("POST", perm=Perm.BOOKING)
def appointment_action(request, pk, action):
    return {"message": services.move_appointment(request.membership, pk, action)}


@api()
def inventory(request):
    clinic = request.membership.clinic
    zone = services.tz(clinic)
    moves = (StockMovement.objects.filter(clinic=clinic, kind="use").select_related("item", "patient")
             .order_by("-created_at", "-id")[:PAGE])
    return {"stock": stock_payload(clinic),
            "usage": [{"item": m.item.name, "patient": m.patient.name if m.patient else "",
                       "when": m.created_at.astimezone(zone).strftime("%d %b, %H:%M"), "qty": m.quantity,
                       "cost": m.quantity * m.unit_price} for m in moves]}


@api("POST", perm=Perm.INVENTORY)
def stock_purchase(request):
    d = request.data
    return {"message": services.purchase(request.membership, integer(d, "item", 1, 2**62),
                                         integer(d, "quantity", 1, 100000), integer(d, "price", 0, MAX_PRICE))}


@api("POST", perm=Perm.INVENTORY)
def stock_use(request):
    d = request.data
    return {"message": services.use_stock(request.membership, integer(d, "item", 1, 2**62), text(d, "patient", 120),
                                          integer(d, "quantity", 1, 100000))}


@api(perm=Perm.FINANCES)
def finances(request):
    clinic = request.membership.clinic
    zone = services.tz(clinic)
    data = services.finances(clinic)
    data["expenses"] = [{"name": e.name, "category": e.category, "amount": e.amount,
                         "date": e.spent_at.astimezone(zone).strftime("%d %b")} for e in clinic.expenses.order_by("-spent_at", "-id")[:PAGE]]
    data["pay"] = [{"name": p.name, "title": p.title, "kind": p.kind, "percent": p.percent, "base": p.base,
                    "amount": p.amount if p.kind == "fixed" else p.base * p.percent // 100} for p in clinic.staff_pay.order_by("id")]
    return data


@api("POST", perm=Perm.FINANCES)
def add_expense(request):
    d = request.data
    services.add_expense(request.membership, text(d, "name", 160), text(d, "category", 12), integer(d, "amount", 1, MAX_PRICE))
    return {"message": "Expense added"}


@api(perm=Perm.RX)
def rx_data(request):
    clinic = request.membership.clinic
    start, end = services.day_bounds(clinic)
    booked = (clinic.appointments.filter(starts_at__gte=start, starts_at__lt=end)
              .exclude(status="cancelled").values("patient_id"))
    patients = clinic.patients.filter(id__in=booked).order_by("name")[:300]
    return {"patients": [{"id": p.id, "name": p.name} for p in patients],
            "medicines": [{"id": m.id, "name": m.name, "form": m.form, "dose": m.dose} for m in clinic.medicines.order_by("id")]}


@api("POST", perm=Perm.RX)
def rx_create(request):
    d = request.data
    raw = d.get("items")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 30:
        raise ApiError("Add at least one medicine first")
    lines = []
    for row in raw:
        if not isinstance(row, dict):
            raise ApiError("Invalid medicine")
        lines.append({"medicine": integer(row, "medicine", 1, 2**62), "quantity": integer(row, "quantity", 1, 200),
                      "days": integer(row, "days", 1, 90)})
    return services.write_prescription(request.membership, integer(d, "patient", 1, 2**62), lines)


@api(perm=Perm.ONLINE)
def consultations(request):
    clinic = request.membership.clinic
    zone = services.tz(clinic)
    rows = clinic.consultations.exclude(status=Consultation.Status.DECLINED).select_related("patient").order_by("scheduled_at")[:PAGE]
    return {"items": [{"id": c.id, "patient": c.patient.name, "reason": c.reason, "status": c.status,
                       "when": c.scheduled_at.astimezone(zone).strftime("%d %b, %H:%M")} for c in rows]}


@api("POST", perm=Perm.ONLINE)
def consultation_action(request, pk, action):
    name = services.move_consultation(request.membership, pk, action)
    return {"message": {"accept": f"Request accepted — {name}", "decline": "Request declined",
                        "start": f"Video call marked as started with {name}", "end": "Call finished"}[action]}


@api(perm=Perm.TEAM)
def team(request):
    clinic = request.membership.clinic
    stored = {(r.role, r.perm): r.allowed for r in clinic.role_permissions.all()}
    return {"specialty": clinic.specialty.name,
            "specialties": [{"name": s.name, "tag": s.tag, "services": s.services, "features": s.features} for s in Specialty.objects.order_by("id")],
            "people": [{"name": m.display_name, "title": m.title, "note": m.note} for m in clinic.members.order_by("id")],
            "perms": [{"id": p.value, "name": p.label, "assistant": stored.get((Role.ASSISTANT, p.value), False),
                       "associate": stored.get((Role.ASSOCIATE, p.value), False)} for p in Perm]}


@api("POST", perm=Perm.TEAM)
def team_specialty(request):
    services.set_specialty(request.membership, text(request.data, "name", 60))
    return {"message": "Specialty updated across the app"}


@api("POST", perm=Perm.TEAM)
def team_permission(request):
    d = request.data
    allowed = d.get("allowed")
    if not isinstance(allowed, bool):
        raise ApiError("allowed must be true or false")
    services.set_permission(request.membership, text(d, "role", 12), text(d, "perm", 12), allowed)
    return {"message": "Permission updated"}
