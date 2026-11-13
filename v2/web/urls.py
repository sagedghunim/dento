"""M1 pages and fragments (design 7.3). The M0 pages stay in config/urls.py."""

from django.urls import path

from . import finance_pages, inventory_pages, overview_pages, pages, rx_pages, team_pages, teeth, visit_pages

urlpatterns = [
    path("today", pages.today_page, name="today"),
    path("today/queue", pages.today_queue, name="today-queue"),
    path("today/walk-in", pages.walk_in_defaults, name="walk-in-defaults"),
    path("patients", pages.patients_page, name="patients"),
    path("patients/search", pages.patient_search, name="patient-search"),
    path("patients/<int:patient_id>", pages.patient_page, name="patient"),
    path("patients/<int:patient_id>/visits", pages.patient_visits_more, name="patient-visits"),
    path("patients/<int:patient_id>/payments", pages.patient_payments_more, name="patient-payments"),
    path("visits/<int:appointment_id>", visit_pages.visit_page, name="visit"),
    path("visits/<int:appointment_id>/chart", visit_pages.visit_chart, name="visit-chart"),
    path("visits/<int:appointment_id>/teeth", teeth.visit_teeth_page, name="visit-teeth"),
    path("patients/<int:patient_id>/teeth", teeth.patient_teeth_page, name="patient-teeth"),
    path("patients/<int:patient_id>/teeth/<int:tooth>/history", teeth.tooth_history, name="tooth-history"),
    path("inventory", inventory_pages.inventory_page, name="inventory"),
    path("inventory/items", inventory_pages.inventory_items, name="inventory-items"),
    path("inventory/items/<int:item_id>/movements", inventory_pages.item_movements, name="item-movements"),
    path("overview", overview_pages.overview_page, name="overview"),
    path("finances", finance_pages.finances_page, name="finances"),
    path("finances/part", finance_pages.finances_part, name="finances-part"),
    path("finances/expenses", finance_pages.expense_rows, name="finances-expenses"),
    path("finances/shifts", finance_pages.shift_rows, name="finances-shifts"),
    path("finances/shifts/<int:shift_id>", finance_pages.shift_report, name="finances-shift"),
    path("finances/owed", finance_pages.owed_rows, name="finances-owed"),
    path("finances/pay/<int:membership_id>", finance_pages.statement, name="finances-statement"),
    path("prescriptions", rx_pages.prescriptions_page, name="prescriptions"),
    path("prescriptions/medicines", rx_pages.medicine_rows, name="medicine-rows"),
    path("prescriptions/templates", rx_pages.template_rows, name="template-rows"),
    path("print/receipts/<int:invoice_id>", pages.receipt_page, name="receipt"),
    path("signup", pages.signup_page, name="signup"),
    path("places/new", pages.place_new_page, name="place-new"),
    path("team", team_pages.team_page, name="team"),
]
