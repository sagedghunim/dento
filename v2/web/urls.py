"""M1 pages and fragments (design 7.3). The M0 pages stay in config/urls.py."""

from django.urls import path

from . import inventory_pages, pages, rx_pages, teeth

urlpatterns = [
    path("today", pages.today_page, name="today"),
    path("today/queue", pages.today_queue, name="today-queue"),
    path("patients", pages.patients_page, name="patients"),
    path("patients/search", pages.patient_search, name="patient-search"),
    path("patients/<int:patient_id>", pages.patient_page, name="patient"),
    path("patients/<int:patient_id>/visits", pages.patient_visits_more, name="patient-visits"),
    path("patients/<int:patient_id>/payments", pages.patient_payments_more, name="patient-payments"),
    path("visits/<int:appointment_id>/teeth", teeth.visit_teeth_page, name="visit-teeth"),
    path("patients/<int:patient_id>/teeth", teeth.patient_teeth_page, name="patient-teeth"),
    path("patients/<int:patient_id>/teeth/<int:tooth>/history", teeth.tooth_history, name="tooth-history"),
    path("inventory", inventory_pages.inventory_page, name="inventory"),
    path("inventory/items", inventory_pages.inventory_items, name="inventory-items"),
    path("inventory/items/<int:item_id>/movements", inventory_pages.item_movements, name="item-movements"),
    path("prescriptions", rx_pages.prescriptions_page, name="prescriptions"),
    path("prescriptions/medicines", rx_pages.medicine_rows, name="medicine-rows"),
    path("prescriptions/templates", rx_pages.template_rows, name="template-rows"),
    path("print/receipts/<int:invoice_id>", pages.receipt_page, name="receipt"),
    path("signup", pages.signup_page, name="signup"),
    path("places/new", pages.place_new_page, name="place-new"),
]
