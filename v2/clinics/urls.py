"""Clinic configuration routes: sections and sessions (design 6.2, D1, D2)."""

from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/sections", views.sections_view),
    path("api/v1/sections/<int:section_id>/services", views.section_services_view),
    path("api/v1/sections/<int:section_id>/doctors", views.section_doctors_view),
    path("api/v1/schedules", views.schedules_endpoint),
    path("api/v1/schedules/<int:schedule_id>", views.schedule_endpoint),
]
