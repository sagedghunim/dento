"""Appointment and Today routes (design 6.2)."""

from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/today", views.today_view),
    path("api/v1/appointments", views.book_view),
    path("api/v1/appointments/quote", views.quote_view),
    path("api/v1/appointments/<int:appointment_id>", views.appointment_view),
    path("api/v1/appointments/<int:appointment_id>/arrive", views.arrive_view),
    path("api/v1/appointments/<int:appointment_id>/undo-arrive", views.undo_arrive_view),
    path("api/v1/appointments/<int:appointment_id>/call", views.call_view),
    path("api/v1/appointments/<int:appointment_id>/undo-call", views.undo_call_view),
    path("api/v1/appointments/<int:appointment_id>/finish", views.finish_view),
    path("api/v1/appointments/<int:appointment_id>/cancel", views.cancel_view),
    path("api/v1/appointments/<int:appointment_id>/reschedule", views.reschedule_view),
    path("api/v1/doctors/<int:doctor_id>/slots", views.slots_view),
]
