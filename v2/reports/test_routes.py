"""Every M3 route declares its permission code (design F: "each action takes its own permission code")."""

from django.urls import get_resolver

from clinical.testing import M2TestCase
from core.testing import call

EXPECTED = {
    # F1 register
    ("GET", "api/v1/register"): ("cash.collect",),
    ("POST", "api/v1/register/open"): ("cash.shift",),
    ("POST", "api/v1/register/movements"): ("cash.shift",),
    ("GET", "api/v1/register/shifts"): ("cash.shift",),
    ("GET", "api/v1/register/shifts/<int:shift_id>"): ("cash.shift",),
    ("POST", "api/v1/register/shifts/<int:shift_id>/close"): ("cash.shift",),
    # F3 expenses
    ("GET", "api/v1/expense-categories"): ("expenses.manage",),
    ("POST", "api/v1/expense-categories"): ("expenses.manage",),
    ("PATCH", "api/v1/expense-categories/<int:category_id>"): ("expenses.manage",),
    ("GET", "api/v1/expenses"): ("expenses.manage",),
    ("POST", "api/v1/expenses"): ("expenses.manage",),
    ("POST", "api/v1/expenses/<int:expense_id>/void"): ("expenses.manage",),
    ("POST", "api/v1/drawer-expenses"): ("cash.shift",),  # the desk pays from the drawer (Saged, 2026-11-22)
    # F4 staff pay, F5 reports
    ("GET", "api/v1/finances/pay"): ("finances.view",),
    ("PUT", "api/v1/finances/pay/<int:membership_id>"): ("finances.pay",),
    ("GET", "api/v1/finances/pay/<int:membership_id>/statement"): ("finances.view",),
    ("POST", "api/v1/finances/pay/<int:membership_id>/payouts"): ("finances.pay",),
    ("GET", "api/v1/finances/receivables"): ("finances.view",),
    ("GET", "api/v1/finances/revenue"): ("finances.view",),
    ("GET", "api/v1/finances/profit"): ("finances.view",),
    # F2 stock
    ("GET", "api/v1/inventory/items"): ("inventory.use",),
    ("POST", "api/v1/inventory/items"): ("inventory.manage",),
    ("PATCH", "api/v1/inventory/items/<int:item_id>"): ("inventory.manage",),
    ("GET", "api/v1/inventory/items/<int:item_id>/movements"): ("inventory.use",),
    ("POST", "api/v1/inventory/items/<int:item_id>/purchases"): ("inventory.manage",),
    ("POST", "api/v1/inventory/items/<int:item_id>/count"): ("inventory.manage",),
    ("POST", "api/v1/inventory/items/<int:item_id>/waste"): ("inventory.manage",),
    ("POST", "api/v1/inventory/uses"): ("inventory.use",),
    ("GET", "api/v1/inventory/materials/<int:procedure_id>"): ("inventory.use",),
    ("PUT", "api/v1/inventory/materials/<int:procedure_id>"): ("inventory.manage",),
    ("POST", "api/v1/inventory/defaults"): ("inventory.manage",),
    # F6, F8
    ("GET", "api/v1/reports/insights"): ("reports.view",),
    ("GET", "api/v1/reports/patients/<slug:name>"): ("reports.view",),
    ("GET", "api/v1/reports/patients/<slug:name>/export"): ("reports.view", "patients.export"),
    ("GET", "api/v1/reports/performance"): ("reports.view",),
    ("GET", "api/v1/reports/staff"): ("reports.view",),
}
OTHER = {
    # F7 packages (billing.package_urls), F6 tags (patients.tag_urls)
    ("GET", "api/v1/packages"): ("billing.collect",),
    ("POST", "api/v1/packages"): ("packages.manage",),
    ("PATCH", "api/v1/packages/<int:package_id>"): ("packages.manage",),
    ("POST", "api/v1/appointments/<int:appointment_id>/packages"): ("billing.collect",),
    ("GET", "api/v1/patients/<int:patient_id>/packages"): ("patients.view",),
    ("GET", "api/v1/patient-tags"): ("patients.view",),
    ("POST", "api/v1/patient-tags"): ("patients.tags",),
    ("PATCH", "api/v1/patient-tags/<int:tag_id>"): ("patients.tags",),
    ("GET", "api/v1/patients/<int:patient_id>/tags"): ("patients.view",),
    ("PUT", "api/v1/patients/<int:patient_id>/tags"): ("patients.edit",),
}


def routes(modules):
    found = {}
    for pattern in get_resolver().url_patterns:
        if getattr(getattr(pattern, "urlconf_module", None), "__name__", "") not in modules:
            continue
        for route in pattern.url_patterns:
            views = getattr(route.callback, "api_views", None) or {route.callback.api_meta["method"]: route.callback}
            for method, view in views.items():
                found[method, str(route.pattern)] = view.api_meta["perm"]
    return found


class M3RouteTests(M2TestCase):
    def test_every_m3_route_declares_its_codes(self):
        self.assertEqual(routes({"finance.urls", "inventory.urls", "reports.urls"}), EXPECTED)
        self.assertEqual(routes({"billing.package_urls", "patients.tag_urls"}), OTHER)

    def test_a_member_without_the_code_is_refused_on_every_m3_route(self):
        """The assistant holds none of the owner's back-office codes: every such route is 403, never a 500."""
        from clinics.permissions import TEMPLATES

        held = TEMPLATES["assistant"]
        for (method, pattern), codes in {**EXPECTED, **OTHER}.items():
            if set(codes) <= held:
                continue
            path = "/" + pattern.replace("<slug:name>", "never_came_back")
            for part in ("shift_id", "category_id", "expense_id", "membership_id", "item_id", "procedure_id",
                         "package_id", "appointment_id", "patient_id", "tag_id"):
                path = path.replace(f"<int:{part}>", "1")
            r = call(self.desk, method, path, {})
            self.assertEqual(r.status_code, 403, (method, path, r.content))
