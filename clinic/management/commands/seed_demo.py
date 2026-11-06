"""Fake demo data for local use. Refuses to run outside DEBUG."""
import secrets
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from clinic import services
from clinic.models import (Activity, Appointment, Clinic, Consultation, Expense, Medicine, Membership, Patient, Payment,
                           Perm, RolePermission, Specialty, StaffPay, StockItem, StockMovement)

SPECIALTIES = [
    ("Dental", "Dentists", ["Checkup & X-ray", "Cleaning", "Filling", "Root canal", "Extraction", "Implants", "Braces follow-up", "Whitening"], ["A tooth chart on every patient file", "Anesthesia and implant stock tracked per patient", "Lab orders for crowns and dentures"]),
    ("Dermatology", "Skin clinics", ["Skin check", "Acne follow-up", "Laser session", "Peeling session"], ["Session packages that count down on their own", "Before-and-after photos on the patient file", "Laser and cream stock tracking"]),
    ("Pediatrics", "Children's clinics", ["Well-child visit", "Vaccination", "Follow-up", "Fever visit"], ["Growth chart on every child file", "Vaccination schedule with due-date reminders", "Weight-based medicine doses in prescriptions"]),
    ("Physiotherapy", "Physio centers", ["Assessment", "Exercise session", "Electrotherapy", "Follow-up"], ["Treatment plans split into sessions", "Progress notes per session", "Equipment and consumables stock"]),
    ("Internal medicine", "Internists", ["General checkup", "Diabetes follow-up", "Blood pressure check", "Lab review"], ["Long-term patient files with lab history", "Repeat prescriptions in two taps", "Chronic-care recall lists"]),
    ("Ophthalmology", "Eye clinics", ["Eye exam", "Vision test", "Glasses fitting", "Follow-up"], ["Vision measurements saved per visit", "Lens and drops stock tracking", "Surgery day scheduling"]),
]
DEFAULTS = {"finances": (False, True), "inventory": (True, True), "booking": (True, True), "rx": (False, True), "online": (False, True), "team": (False, False)}


def ensure_specialties():
    for name, tag, svc, feat in SPECIALTIES:
        Specialty.objects.get_or_create(name=name, defaults={"tag": tag, "services": svc, "features": feat})


def default_permissions(clinic):
    for perm, (assistant, associate) in DEFAULTS.items():
        RolePermission.objects.update_or_create(clinic=clinic, role="assistant", perm=perm, defaults={"allowed": assistant})
        RolePermission.objects.update_or_create(clinic=clinic, role="associate", perm=perm, defaults={"allowed": associate})


class Command(BaseCommand):
    help = "Create the demo clinic with fake data."

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DJANGO_DEBUG=1")
        ensure_specialties()
        if Clinic.objects.exists():
            self.stdout.write("Demo data already exists.")
            return
        password = secrets.token_urlsafe(9)
        users = get_user_model().objects
        clinic = Clinic.objects.create(name="Smile Dental Center", city="Cairo", specialty=Specialty.objects.get(name="Dental"))
        owner = Membership.objects.create(clinic=clinic, user=users.create_user("hamada", password=password), role="owner", display_name="Dr. Hamada Aziz", title="Doctor — owner", note="Full access. Manages settings, team and permissions.")
        asst = Membership.objects.create(clinic=clinic, user=users.create_user("sara", password=password), role="assistant", display_name="Sara Mahmoud", title="Assistant", note="Patients, bookings and day-to-day follow-up.")
        assoc = Membership.objects.create(clinic=clinic, user=users.create_user("ahmed", password=password), role="associate", display_name="Dr. Ahmed Samy", title="Associate dentist", note="Sees his own patients and writes prescriptions.")
        default_permissions(clinic)

        today = services.local_now(clinic).date()
        zone = services.tz(clinic)
        for name, service, doc, hh, mm, status, new in [
            ("Omar Khalil", "Root canal", owner, 10, 0, "room", False), ("Salma Nasser", "Filling", assoc, 10, 30, "waiting", False),
            ("Youssef Adel", "Extraction", owner, 11, 0, "waiting", False), ("Mariam Fathy", "Cleaning", assoc, 11, 30, "booked", False),
            ("Karim Adel", "Checkup & X-ray", owner, 12, 0, "booked", False), ("Hana Mostafa", "Braces follow-up", assoc, 12, 30, "booked", True),
            ("Layla Samir", "Whitening", owner, 9, 0, "done", False),
        ]:
            Appointment.objects.create(clinic=clinic, patient=services.patient_named(clinic.id, name), service=service, doctor=doc,
                                       starts_at=datetime.combine(today, time(hh, mm), zone), status=status, is_new=new, fee=50000)

        now = timezone.now()
        items = {}
        for name, unit, stock, threshold, price, pack in [
            ("Anesthesia injections", "injections", 8, 2, 45, "Box of 50 carpules"), ("Composite filling syringes", "syringes", 14, 4, 320, "4 g syringe"),
            ("Glove boxes", "boxes", 9, 3, 180, "100 gloves per box"), ("Sterile mirror packs", "packs", 22, 6, 25, "Single-use pack"),
            ("Implant fixtures", "fixtures", 5, 2, 1450, "Titanium, assorted sizes"), ("Cotton roll packs", "packs", 18, 5, 60, "Pack of 200 rolls"),
        ]:
            items[name] = StockItem.objects.create(clinic=clinic, name=name, unit=unit, quantity=stock, threshold=threshold, unit_price=price * 100, pack=pack)
        for item, patient, qty in [("Anesthesia injections", "Omar Khalil", 2), ("Composite filling syringes", "Salma Nasser", 1), ("Anesthesia injections", "Youssef Adel", 1)]:
            StockMovement.objects.create(clinic=clinic, item=items[item], kind="use", quantity=qty, unit_price=items[item].unit_price,
                                         patient=services.patient_named(clinic.id, patient), created_by=asst, created_at=now)
        for name, cat, amount in [("Clinic rent", "Rent", 15000), ("Staff salaries", "Salaries", 31700), ("Stock purchases", "Inventory", 12750),
                                  ("Electricity bill", "Electricity", 2350), ("Cleaning & laundry", "Other", 1180), ("Internet & phone", "Other", 640)]:
            Expense.objects.create(clinic=clinic, name=name, category=cat, amount=amount * 100, spent_at=now, created_by=owner)
        Payment.objects.create(clinic=clinic, amount=86400_00, received_at=now)
        StaffPay.objects.create(clinic=clinic, name="Dr. Ahmed Samy", title="Associate dentist", kind="percent", percent=30, base=24000_00)
        StaffPay.objects.create(clinic=clinic, name="Dr. Nour El-Din", title="Implant specialist", kind="fixed", amount=18000_00)
        StaffPay.objects.create(clinic=clinic, name="Sara Mahmoud", title="Assistant", kind="fixed", amount=6500_00)
        for name, form, dose in [("Amoxicillin 500 mg", "Capsules", "1 capsule, 3 times a day"), ("Ibuprofen 400 mg", "Tablets", "1 tablet when in pain, after food"),
                                 ("Paracetamol 500 mg", "Tablets", "1–2 tablets, up to 4 times a day"), ("Metronidazole 500 mg", "Tablets", "1 tablet, 3 times a day"),
                                 ("Clindamycin 300 mg", "Capsules", "1 capsule, 4 times a day"), ("Chlorhexidine 0.12%", "Mouthwash bottle", "Rinse for 1 minute, twice a day")]:
            Medicine.objects.create(clinic=clinic, name=name, form=form, dose=dose)
        for name, why, hours, status in [("Farah Nabil", "Tooth pain since yesterday — wants advice before booking", 2, "new"),
                                         ("Mostafa Kamel", "Asking about implant steps and total cost", 4, "accepted"),
                                         ("Dina Sherif", "Follow-up one day after an extraction", 24, "accepted")]:
            Consultation.objects.create(clinic=clinic, patient=services.patient_named(clinic.id, name), reason=why, scheduled_at=now + timedelta(hours=hours), status=status)
        Activity.objects.create(clinic=clinic, action="seed", text="Demo clinic created", created_at=now)
        self.stdout.write(self.style.SUCCESS(f"Demo logins: hamada, sara, ahmed. Password for all three: {password}"))
