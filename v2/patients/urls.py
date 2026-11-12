"""Patient routes (design 6.2)."""

from django.urls import path

from core.api import by_method

from . import views

urlpatterns = [
    path("api/v1/patients", by_method(GET=views.search_view, POST=views.create_view)),
    path("api/v1/patients/by-phone", views.by_phone_view),
    path("api/v1/patients/<int:patient_id>", by_method(GET=views.file_view, PATCH=views.update_view)),
    path("api/v1/patients/<int:patient_id>/visits", views.visits_view),
    path("api/v1/patients/<int:patient_id>/payments", views.payments_view),
]
