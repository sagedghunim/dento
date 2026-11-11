"""Platform admin routes (design 5.12, 6.2)."""

from django.urls import path

from . import views

urlpatterns = [
    path("platform", views.platform_page, name="platform"),
    path("api/v1/platform/clinics", views.clinics_endpoint),
    path("api/v1/platform/clinics/<str:clinic_id>/suspend", views.clinics_suspend),
    path("api/v1/platform/clinics/<str:clinic_id>/reactivate", views.clinics_reactivate),
    path("api/v1/signup", views.signup_view),
    path("api/v1/me/places", views.places_view),
]
