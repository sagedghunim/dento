"""Installment routes (Saged, 2026-11-19); billing.test_installments checks them."""

from django.urls import path

from core.api import by_method

from . import views

urlpatterns = [
    path("api/v1/patients/<int:patient_id>/installments",
         by_method(GET=views.installments_view, POST=views.installments_make_view, DELETE=views.installments_cancel_view)),
    path("api/v1/patients/<int:patient_id>/installments/payments", views.installments_pay_view),
]
