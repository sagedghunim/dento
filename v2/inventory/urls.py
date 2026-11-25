"""Stock routes (design 5.4, F2)."""

from django.urls import path

from core.api import by_method

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
    path("api/v1/inventory/kits", by_method(GET=views.kits_view, POST=views.create_kit_view)),
    path("api/v1/inventory/kits/<int:kit_id>", by_method(PUT=views.update_kit_view, DELETE=views.delete_kit_view)),
]
