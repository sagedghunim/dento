"""Session package routes (design F7), kept apart from M1's billing routes and their endpoint matrix."""

from django.urls import path

from core.api import by_method

from . import views

urlpatterns = [
    path("api/v1/packages", by_method(GET=views.packages_view, POST=views.create_package_view)),
    path("api/v1/packages/<int:package_id>", views.update_package_view),
    path("api/v1/appointments/<int:appointment_id>/packages", views.sell_package_view),
    path("api/v1/patients/<int:patient_id>/packages", views.patient_packages_view),
]
