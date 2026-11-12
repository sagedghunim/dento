"""Patient tag routes (design F6), kept apart from M1's patient routes and their endpoint matrix."""

from django.urls import path

from core.api import by_method

from . import views

urlpatterns = [
    path("api/v1/patients/<int:patient_id>/tags", by_method(GET=views.patient_tags_view, PUT=views.set_patient_tags_view)),
    path("api/v1/patient-tags", by_method(GET=views.tags_view, POST=views.create_tag_view)),
    path("api/v1/patient-tags/<int:tag_id>", views.update_tag_view),
]
