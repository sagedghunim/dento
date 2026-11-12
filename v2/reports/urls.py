"""Insights and performance routes (design F6, F8)."""

from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/reports/insights", views.insights_view),
    path("api/v1/reports/patients/<slug:name>", views.patient_list_view),
    path("api/v1/reports/patients/<slug:name>/export", views.export_view),
    path("api/v1/reports/performance", views.performance_view),
    path("api/v1/reports/staff", views.staff_performance_view),
]
