"""Deleting a patient's file and bringing it back (Saged, 2026-11-19): the owner only; patients.test_delete checks them."""

from django.urls import path

from . import delete_views

urlpatterns = [
    path("api/v1/patients/<int:patient_id>/delete", delete_views.delete_view),
    path("api/v1/patients/<int:patient_id>/restore", delete_views.restore_view),
]
