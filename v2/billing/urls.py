"""Billing routes (design 6.2)."""

from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/appointments/<int:appointment_id>/payments", views.pay_view),
    path("api/v1/appointments/<int:appointment_id>/invoice", views.visit_invoice_view),
    path("api/v1/invoices/<int:invoice_id>", views.invoice_view),
    path("api/v1/invoices/<int:invoice_id>/lines", views.add_line_view),
    path("api/v1/invoices/<int:invoice_id>/lines/<int:line_id>", views.remove_line_view),
    path("api/v1/invoices/<int:invoice_id>/discount", views.discount_view),
    path("api/v1/invoices/<int:invoice_id>/void", views.void_view),
    path("api/v1/payments/<int:payment_id>/refund", views.refund_view),
]
