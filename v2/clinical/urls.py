"""The doctor's visit routes (design 6.2 Clinical, E3)."""

from django.urls import path

from core.api import by_method

from . import views

v = "api/v1/"

urlpatterns = [
    # The visit record
    path(v + "appointments/<int:appointment_id>/record", views.record_view),
    path(v + "appointments/<int:appointment_id>/note", by_method(GET=views.note_view, PUT=views.note_save_view)),
    path(v + "appointments/<int:appointment_id>/vitals", by_method(GET=views.vitals_view, PUT=views.vitals_save_view)),
    path(v + "appointments/<int:appointment_id>/procedures", views.record_procedure_view),
    # The patient's clinical file
    path(v + "patients/<int:patient_id>/timeline", views.timeline_view),
    path(v + "patients/<int:patient_id>/balance", views.balance_view),
    path(v + "patients/<int:patient_id>/attachments",
         by_method(GET=views.attachments_view, POST=views.attachment_add_view)),
    path(v + "attachments/<int:attachment_id>", views.attachment_file_view),
    path(v + "patients/<int:patient_id>/prescriptions", views.patient_prescriptions_view),
    path(v + "patients/<int:patient_id>/allergy-check", views.allergy_check_view),
    path(v + "patients/<int:patient_id>/teeth", views.chart_view),
    path(v + "patients/<int:patient_id>/teeth/<int:tooth>", views.tooth_set_view),
    path(v + "patients/<int:patient_id>/teeth/<int:tooth>/history", views.tooth_history_view),
    path(v + "patients/<int:patient_id>/plans", by_method(GET=views.plans_view, POST=views.plan_create_view)),
    path(v + "plans/<int:plan_id>/items", views.plan_items_view),
    path(v + "plan-items/<int:item_id>/status", views.plan_item_status_view),
    # Prescriptions
    path(v + "prescriptions", views.prescription_create_view),
    path(v + "prescriptions/<int:prescription_id>", views.prescription_view),
    # The section libraries: medicines, prescription templates, procedures
    path(v + "medicines", by_method(GET=views.medicines_view, POST=views.medicine_create_view)),
    path(v + "medicines/<int:medicine_id>",
         by_method(PATCH=views.medicine_update_view, DELETE=views.medicine_remove_view)),
    path(v + "prescription-templates", by_method(GET=views.templates_view, POST=views.template_create_view)),
    path(v + "prescription-templates/<int:template_id>",
         by_method(PUT=views.template_update_view, DELETE=views.template_remove_view)),
    path(v + "procedures", by_method(GET=views.procedures_view, POST=views.procedure_create_view)),
    path(v + "procedures/<int:procedure_id>",
         by_method(PATCH=views.procedure_update_view, DELETE=views.procedure_remove_view)),
]
