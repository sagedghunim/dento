"""Bringing back a file hidden before deleting a patient was taken out (Saged, 2026-11-26); the owner only."""

from django.urls import path

from . import delete_views

urlpatterns = [
    path("api/v1/patients/<int:patient_id>/restore", delete_views.restore_view),
]
