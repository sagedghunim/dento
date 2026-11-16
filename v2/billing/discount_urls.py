"""Discount type routes (Saged, 2026-11-13), kept apart from M1's billing routes and their endpoint matrix;
billing.test_discount_types checks every one."""

from django.urls import path

from core.api import by_method

from . import views

urlpatterns = [
    path("api/v1/discount-types", by_method(GET=views.discount_types_view, POST=views.create_discount_type_view)),
    path("api/v1/discount-types/order", views.order_discount_types_view),
    path("api/v1/discount-types/<int:type_id>", views.update_discount_type_view),
    path("api/v1/appointments/<int:appointment_id>/discount-types", views.visit_discount_types_view),
    path("api/v1/invoices/<int:invoice_id>/discount-type", views.discount_type_view),
    path("api/v1/appointments/<int:appointment_id>/suggested-discount", views.suggest_discount_view),
]
