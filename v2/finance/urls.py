"""Back-office money routes (design F1, F3, F4, F5)."""

from django.urls import path

from core.api import by_method

from . import views

urlpatterns = [
    # The register (F1)
    path("api/v1/register", views.register_view),
    path("api/v1/register/open", views.open_shift_view),
    path("api/v1/register/movements", views.cash_movement_view),
    path("api/v1/register/shifts", views.shifts_view),
    path("api/v1/register/shifts/<int:shift_id>", views.shift_view),
    path("api/v1/register/shifts/<int:shift_id>/close", views.close_shift_view),
    # Expenses (F3)
    path("api/v1/expense-categories", by_method(GET=views.categories_view, POST=views.create_category_view)),
    path("api/v1/expense-categories/<int:category_id>", views.update_category_view),
    path("api/v1/expenses", by_method(GET=views.expenses_view, POST=views.add_expense_view)),
    path("api/v1/expenses/<int:expense_id>/void", views.void_expense_view),
    # Staff pay (F4)
    path("api/v1/finances/pay", views.pay_rules_view),
    path("api/v1/finances/pay/<int:membership_id>", views.set_pay_rule_view),
    path("api/v1/finances/pay/<int:membership_id>/statement", views.statement_view),
    path("api/v1/finances/pay/<int:membership_id>/payouts", views.payout_view),
    # Money reports (F5)
    path("api/v1/finances/receivables", views.receivables_view),
    path("api/v1/finances/revenue", views.revenue_view),
    path("api/v1/finances/profit", views.profit_view),
]
