from django.urls import path

from . import views as v

urlpatterns = [
    path("", v.app),
    path("healthz", v.healthz),
    path("api/login", v.api_login),
    path("api/logout", v.api_logout),
    path("api/me", v.me),
    path("api/today", v.today),
    path("api/appointments", v.book),
    path("api/appointments/<int:pk>/<str:action>", v.appointment_action),
    path("api/inventory", v.inventory),
    path("api/stock/purchase", v.stock_purchase),
    path("api/stock/use", v.stock_use),
    path("api/finances", v.finances),
    path("api/expenses", v.add_expense),
    path("api/rx", v.rx_data),
    path("api/prescriptions", v.rx_create),
    path("api/consultations", v.consultations),
    path("api/consultations/<int:pk>/<str:action>", v.consultation_action),
    path("api/team", v.team),
    path("api/team/specialty", v.team_specialty),
    path("api/team/permission", v.team_permission),
]
