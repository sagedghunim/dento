"""M1 pages and fragments (design 7.3). The M0 pages stay in config/urls.py."""

from django.urls import path

from . import pages

urlpatterns = [
    path("today", pages.today_page, name="today"),
    path("today/queue", pages.today_queue, name="today-queue"),
    path("patients", pages.patients_page, name="patients"),
    path("patients/search", pages.patient_search, name="patient-search"),
    path("patients/<int:patient_id>", pages.patient_page, name="patient"),
    path("patients/<int:patient_id>/visits", pages.patient_visits_more, name="patient-visits"),
    path("patients/<int:patient_id>/payments", pages.patient_payments_more, name="patient-payments"),
    path("print/receipts/<int:invoice_id>", pages.receipt_page, name="receipt"),
    path("signup", pages.signup_page, name="signup"),
    path("places/new", pages.place_new_page, name="place-new"),
]
