"""All business rules. Views only parse input and call these, inside one transaction."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import F, Sum
from django.utils import timezone

from .http import ApiError
from .models import (Activity, Appointment, Consultation, Expense, Medicine, Patient, Payment, Perm,
                     Prescription, PrescriptionItem, Role, RolePermission, Specialty, StockItem, StockMovement)

MAX_MONEY = 100_000_000_00


def tz(clinic):
    return ZoneInfo(clinic.timezone)


def local_now(clinic):
    return timezone.now().astimezone(tz(clinic))


def day_bounds(clinic):
    start = datetime.combine(local_now(clinic).date(), time.min, tz(clinic))
    return start, start + timedelta(days=1)


def month_bounds(clinic):
    first = local_now(clinic).date().replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return datetime.combine(first, time.min, tz(clinic)), datetime.combine(following, time.min, tz(clinic))


def egp(piastres):
    value = piastres / 100
    return f"EGP {value:,.0f}" if piastres % 100 == 0 else f"EGP {value:,.2f}"


def note(member, action, text, sensitive=False):
    Activity.objects.create(clinic_id=member.clinic_id, actor=member, action=action, text=text,
                            sensitive=sensitive, created_at=timezone.now())


def patient_named(clinic_id, name):
    found = Patient.objects.filter(clinic_id=clinic_id, name=name).order_by("id").first()
    return found or Patient.objects.create(clinic_id=clinic_id, name=name)


def book(member, name, service, hhmm, source, fee):
    clinic = member.clinic
    if service not in clinic.specialty.services:
        raise ApiError("Pick a service from the list")
    if source not in Appointment.Source.values:
        raise ApiError("Pick how the booking was made")
    try:
        at = datetime.strptime(hhmm, "%H:%M").time()
    except ValueError:
        raise ApiError("Enter a valid time")
    starts = datetime.combine(local_now(clinic).date(), at, tz(clinic))
    appt = Appointment.objects.create(
        clinic=clinic, patient=patient_named(clinic.id, name), service=service, starts_at=starts,
        source=source, fee=fee, is_new=True,
        doctor=member if member.role != Role.ASSISTANT else clinic.members.filter(role=Role.OWNER).first(),
    )
    note(member, "booking", f"New booking ({Appointment.Source(source).label.lower()}): {name} — {service}, {hhmm}")
    return appt


APPT_MOVES = {
    "checkin": (Appointment.Status.BOOKED, Appointment.Status.WAITING),
    "cancel": (Appointment.Status.BOOKED, Appointment.Status.CANCELLED),
    "callin": (Appointment.Status.WAITING, Appointment.Status.ROOM),
    "finish": (Appointment.Status.ROOM, Appointment.Status.DONE),
}


def move_appointment(member, appt_id, action):
    if action not in APPT_MOVES:
        raise ApiError("Unknown action", 404)
    src, dst = APPT_MOVES[action]
    scoped = Appointment.objects.filter(clinic_id=member.clinic_id, pk=appt_id)
    # The status filter makes two clicks or two staff race safely: only one update matches.
    if not scoped.filter(status=src).update(status=dst, is_new=False):
        if not scoped.exists():
            raise ApiError("Appointment not found", 404)
        raise ApiError("This appointment has already changed. Refresh to see it.", 409)
    appt = scoped.select_related("patient", "doctor").get()
    who = appt.patient.name
    if action == "checkin":
        message = f"{who} checked in and is waiting"
    elif action == "cancel":
        message = f"Booking cancelled: {who}"
    elif action == "callin":
        doctor = appt.doctor.display_name if appt.doctor else "the doctor"
        message = f"{who} is in the chair with {doctor}"
    else:
        message = f"{who}'s visit finished — record any materials used in Inventory"
        if appt.fee:
            Payment.objects.get_or_create(appointment=appt, defaults={
                "clinic_id": member.clinic_id, "amount": appt.fee, "received_at": timezone.now()})
    note(member, action, message)
    return message


def purchase(member, item_id, qty, price):
    item = StockItem.objects.filter(clinic_id=member.clinic_id, pk=item_id).first()
    if item is None:
        raise ApiError("Item not found", 404)
    StockItem.objects.filter(pk=item.pk).update(quantity=F("quantity") + qty, unit_price=price)
    now = timezone.now()
    move = StockMovement.objects.create(clinic_id=member.clinic_id, item=item, kind="purchase", quantity=qty,
                                        unit_price=price, created_by=member, created_at=now)
    total = qty * price
    Expense.objects.create(clinic_id=member.clinic_id, name=f"Bought {qty} × {item.name}", category="Inventory",
                           amount=total, spent_at=now, movement=move, created_by=member)
    note(member, "stock_purchase", f"Inventory: bought {qty} {item.unit} of {item.name} for {egp(total)}")
    return f"Stock added: {qty} {item.unit} — and {egp(total)} added to expenses"


def use_stock(member, item_id, patient_name, qty):
    item = StockItem.objects.filter(clinic_id=member.clinic_id, pk=item_id).first()
    if item is None:
        raise ApiError("Item not found", 404)
    # Conditional update: two people using the last unit cannot both succeed.
    if not StockItem.objects.filter(pk=item.pk, quantity__gte=qty).update(quantity=F("quantity") - qty):
        item.refresh_from_db()
        raise ApiError(f"Only {item.quantity} {item.unit} in stock — cannot record {qty}", 409)
    item.refresh_from_db()
    StockMovement.objects.create(clinic_id=member.clinic_id, item=item, kind="use", quantity=qty,
                                 unit_price=item.unit_price, patient=patient_named(member.clinic_id, patient_name),
                                 created_by=member, created_at=timezone.now())
    note(member, "stock_use", f"Inventory: {qty} {item.unit} of {item.name} used for {patient_name}")
    if item.quantity <= item.threshold:
        return f"Recorded. Stock alert: only {item.quantity} {item.unit} of {item.name} left"
    return "Use recorded — stock updated automatically"


def add_expense(member, name, category, amount):
    if category not in Expense.Category.values:
        raise ApiError("Pick an expense type")
    Expense.objects.create(clinic_id=member.clinic_id, name=name, category=category, amount=amount,
                           spent_at=timezone.now(), created_by=member)
    note(member, "expense", f"Expense added: {name}, {egp(amount)}")


def finances(clinic):
    start, end = month_bounds(clinic)
    month = clinic.expenses.filter(spent_at__gte=start, spent_at__lt=end)
    collected = clinic.payments.filter(received_at__gte=start, received_at__lt=end).aggregate(v=Sum("amount"))["v"] or 0
    spent = month.aggregate(v=Sum("amount"))["v"] or 0
    groups = dict(month.order_by().values_list("category").annotate(v=Sum("amount")))
    return {"month": local_now(clinic).strftime("%B"), "collected": collected, "spent": spent,
            "left": collected - spent,
            "bars": [{"name": c, "amount": groups.get(c, 0)} for c in ["Salaries", "Rent", "Inventory", "Electricity", "Other"]]}


def write_prescription(member, patient_id, lines):
    patient = Patient.objects.filter(clinic_id=member.clinic_id, pk=patient_id).first()
    if patient is None:
        raise ApiError("Patient not found", 404)
    meds = {m.id: m for m in Medicine.objects.filter(clinic_id=member.clinic_id, id__in=[l["medicine"] for l in lines])}
    if len(meds) != len({l["medicine"] for l in lines}):
        raise ApiError("Medicine not found", 404)
    rx = Prescription.objects.create(clinic_id=member.clinic_id, patient=patient, doctor=member, created_at=timezone.now())
    PrescriptionItem.objects.bulk_create([
        PrescriptionItem(prescription=rx, medicine_name=meds[l["medicine"]].name, form=meds[l["medicine"]].form,
                         dose=meds[l["medicine"]].dose, quantity=l["quantity"], days=l["days"]) for l in lines])
    note(member, "prescription", f"Prescription {rx.id} written for patient {patient.id}", sensitive=True)
    when = local_now(member.clinic).strftime("%A, %d %B %Y")
    body = "\n".join(f"• {meds[l['medicine']].name} — {meds[l['medicine']].dose}. Amount: {l['quantity']} "
                     f"{meds[l['medicine']].form.lower()}, for {l['days']} days." for l in lines)
    return {"id": rx.id, "patient": patient.name,
            "text": f"PRESCRIPTION\n{member.display_name}\n{member.clinic.name}\n{when}\n\nPatient: {patient.name}\n\n{body}\n\nSignature: ______________________\n"}


CONSULT_MOVES = {
    "accept": ("new", "accepted"), "decline": ("new", "declined"),
    "start": ("accepted", "incall"), "end": ("incall", "done"),
}


def move_consultation(member, consult_id, action):
    if action not in CONSULT_MOVES:
        raise ApiError("Unknown action", 404)
    src, dst = CONSULT_MOVES[action]
    scoped = Consultation.objects.filter(clinic_id=member.clinic_id, pk=consult_id)
    if not scoped.filter(status=src).update(status=dst):
        if not scoped.exists():
            raise ApiError("Request not found", 404)
        raise ApiError("This request has already changed. Refresh to see it.", 409)
    name = scoped.select_related("patient").get().patient.name
    if action == "start":
        note(member, "consult", f"Online consultation started with {name}")
    if action == "end":
        note(member, "consult", f"Online consultation finished with {name}")
    return name


def set_specialty(member, name):
    specialty = Specialty.objects.filter(name=name).first()
    if specialty is None:
        raise ApiError("Specialty not found", 404)
    member.clinic.specialty = specialty
    member.clinic.save(update_fields=["specialty"])
    note(member, "specialty", f"Specialty changed to {name}", sensitive=True)


def set_permission(member, role, perm, allowed):
    if role not in (Role.ASSISTANT, Role.ASSOCIATE) or perm not in Perm.values:
        raise ApiError("Unknown permission")
    if perm == Perm.TEAM and member.role != Role.OWNER:
        raise ApiError("Only the owner can give this permission", 403)
    RolePermission.objects.update_or_create(clinic_id=member.clinic_id, role=role, perm=perm, defaults={"allowed": allowed})
    note(member, "permission", f"{perm} {'on' if allowed else 'off'} for {role}", sensitive=True)
