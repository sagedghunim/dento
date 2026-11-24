"""Appointment and Today routes (design 6.2)."""

from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/today", views.today_view),
    path("api/v1/appointments", views.book_view),
    path("api/v1/appointments/quote", views.quote_view),
    path("api/v1/appointments/walk-in", views.walk_in_view),
    path("api/v1/appointments/<int:appointment_id>", views.appointment_view),
    path("api/v1/appointments/<int:appointment_id>/arrive", views.arrive_view),
    path("api/v1/appointments/<int:appointment_id>/undo-arrive", views.undo_arrive_view),
    path("api/v1/appointments/<int:appointment_id>/assist", views.assist_view),
    path("api/v1/appointments/<int:appointment_id>/undo-assist", views.undo_assist_view),
    path("api/v1/appointments/<int:appointment_id>/ready", views.ready_view),
    path("api/v1/appointments/<int:appointment_id>/undo-ready", views.undo_ready_view),
    path("api/v1/appointments/<int:appointment_id>/wait", views.wait_view),
    path("api/v1/patients/<int:patient_id>/courses", views.courses_view),
    path("api/v1/appointments/<int:appointment_id>/call", views.call_view),
    path("api/v1/appointments/<int:appointment_id>/undo-call", views.undo_call_view),
    path("api/v1/appointments/<int:appointment_id>/finish", views.finish_view),
    path("api/v1/appointments/<int:appointment_id>/reopen", views.reopen_view),
    path("api/v1/appointments/<int:appointment_id>/cancel", views.cancel_view),
    path("api/v1/appointments/<int:appointment_id>/no-show", views.no_show_view),
    path("api/v1/appointments/<int:appointment_id>/undo-no-show", views.undo_no_show_view),
    path("api/v1/appointments/<int:appointment_id>/reschedule", views.reschedule_view),
    path("api/v1/doctors/<int:doctor_id>/slots", views.slots_view),
    path("api/v1/doctors/<int:doctor_id>/month", views.month_view),
]
