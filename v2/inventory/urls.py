"""Stock routes (design 5.4, F2)."""

from django.urls import path

from core.api import by_method

from . import order_views as ov
from . import views

urlpatterns = [
    path("api/v1/inventory/items", by_method(GET=views.items_view, POST=views.create_item_view)),
    path("api/v1/inventory/items/<int:item_id>", views.update_item_view),
    path("api/v1/inventory/items/<int:item_id>/movements", views.movements_view),
    path("api/v1/inventory/items/<int:item_id>/purchases", views.purchase_view),
    path("api/v1/inventory/items/<int:item_id>/count", views.count_view),
    path("api/v1/inventory/items/<int:item_id>/waste", views.waste_view),
    path("api/v1/inventory/uses", views.use_view),
    path("api/v1/inventory/materials/<int:procedure_id>",
         by_method(GET=views.materials_view, PUT=views.set_materials_view)),
    path("api/v1/inventory/defaults", views.defaults_view),
    # Suppliers and purchase orders (design H1).
    path("api/v1/inventory/suppliers", by_method(GET=ov.suppliers_view, POST=ov.create_supplier_view)),
    path("api/v1/inventory/suppliers/<int:supplier_id>", ov.update_supplier_view),
    path("api/v1/inventory/suppliers/<int:supplier_id>/account", ov.account_view),
    path("api/v1/inventory/suppliers/<int:supplier_id>/bills", ov.bill_view),
    path("api/v1/inventory/suppliers/<int:supplier_id>/payments", ov.payment_view),
    path("api/v1/inventory/vendor-entries/<int:entry_id>/void", ov.void_entry_view),
    path("api/v1/inventory/orders", by_method(GET=ov.orders_view, POST=ov.create_order_view)),
    path("api/v1/inventory/orders/refill", ov.refill_view),
    path("api/v1/inventory/orders/<int:order_id>", by_method(GET=ov.order_view, PUT=ov.edit_order_view)),
    path("api/v1/inventory/orders/<int:order_id>/send", ov.send_view),
    path("api/v1/inventory/orders/<int:order_id>/cancel", ov.cancel_view),
    path("api/v1/inventory/orders/<int:order_id>/receive", ov.receive_view),
    path("api/v1/inventory/orders/<int:order_id>/missing", ov.missing_view),
    path("api/v1/inventory/needs", ov.needs_view),
    path("api/v1/inventory/needs/<int:item_id>/order", ov.pick_view),
    path("api/v1/inventory/needs/<int:item_id>/dismiss", ov.dismiss_view),
]
