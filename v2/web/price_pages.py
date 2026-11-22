"""The price list (Saged, 2026-11-22): the procedures of each section, by their main services (Saged's list).

The doctor keeps the procedures (procedures.manage: add, rename, move, take off, bring back); the desk keeps the
prices (prices.edit), and so does a doctor the owner lets see money; the owner both (settings.manage). Actions go
through /api/v1/procedures from prices.js; this file only renders, and the server checks every action again.
"""

import json

from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from core.api import page
from core.i18n import pick

from . import places
from .http import clinic_page

CODES = ("settings.manage", "procedures.manage", "prices.edit")


def unit_labels():
    return {"tooth": _("Per tooth"), "quadrant": _("Per quadrant"), "arch": _("Per jaw"), "unit": _("Per piece"),
            "session": _("Per session"), "case": _("Whole case")}


def own_sections(ctx):
    """The sections a doctor keeps on the price list: those of their sessions (Saged, 2026-11-22: their own services),
    or None for everyone else and for a doctor with no session yet (then the place's one list)."""
    from clinics.models import Schedule

    member = ctx.membership
    if member is None or member.role != "doctor":
        return None
    found = set(Schedule.objects.filter(clinic_id=ctx.clinic_id, doctor_id=member.id).values_list("section_id", flat=True))
    return frozenset(found) or None


def perms(ctx):
    owner = ctx.can("settings.manage")
    return {"manage": owner or ctx.can("procedures.manage"), "price": owner or ctx.can("prices.edit"),
            "money": ctx.can("money.view")}


def groups(ctx, items):
    """The active procedures by section (when there are several), then by main service in Saged's order."""
    from clinics.specialties import FAMILY_LABELS, FAMILY_ORDER

    from .visit_pages import CATEGORY_WORDS

    sections = places.sections(ctx)
    names = {s["id"]: s["name"] for s in sections}
    out = {}
    for p in items:
        if p["family"] in FAMILY_LABELS:
            key, label, rank = p["family"], pick(*FAMILY_LABELS[p["family"]]), (0, FAMILY_ORDER[p["family"]])
        elif p["family"]:  # a main service the clinic added (Saged, 2026-11-22): its name is the key
            key, label, rank = p["family"], p["family"], (0, len(FAMILY_ORDER))
        else:
            words = CATEGORY_WORDS.get(p["category"], ())
            key = f"c-{p['category']}"
            label = pick(words[0], p["category"].replace("_", " ").capitalize()) if words else _("Other")
            rank = (1, 0)
        section = names.get(p["section_id"], "") if len(sections) > 1 else ""
        group = out.setdefault((p["section_id"], key), {"section": section, "label": label, "key": key,
                                                       "rank": (section, *rank, p["sort"]), "items": []})
        group["items"].append(p)
    return sorted(out.values(), key=lambda g: g["rank"])


@require_GET
@ensure_csrf_cookie
@page()
@clinic_page
def prices_page(request, ctx):
    from clinical import selectors
    from clinics.specialties import FAMILY_LABELS

    if not any(ctx.can(code) for code in CODES):
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    found = selectors.procedures(ctx, None, include_inactive=True)["items"]
    mine = own_sections(ctx)
    if mine is not None:
        found = [p for p in found if p["section_id"] in mine]
    if perms(ctx)["price"] and not ctx.can("money.view"):
        # Prices on the price list only, for whoever sets them (a doctor's own services): no other money is shown.
        from core import sql

        prices = dict(sql.rows(ctx, 'SELECT id, price FROM "clinical_procedure" WHERE clinic_id = %s AND id = ANY(%s::bigint[])',
                               [ctx.clinic_id, [p["id"] for p in found]])) if found else {}
        for p in found:
            p["price"] = prices.get(p["id"])
    units = unit_labels()
    for p in found:
        p["unit_label"] = units.get(p["unit"], p["unit"])
        price = p.get("price")
        p["price_input"] = "" if price is None else (f"{price // 100}" if price % 100 == 0 else f"{price / 100:.2f}")
        p["json"] = json.dumps({k: p.get(k) for k in ("id", "name_ar", "name_en", "family", "unit", "price", "section_id")},
                               ensure_ascii=False)
    sections = places.sections(ctx)
    return render(request, "web/prices.html", {
        "ctx": ctx, "nav_active": "prices", "perms": perms(ctx),
        "groups": groups(ctx, [p for p in found if p["is_active"]]),
        "removed": [p for p in found if not p["is_active"]],
        "families": [(key, pick(ar, en)) for key, (ar, en) in FAMILY_LABELS.items()],
        "own_families": sorted({p["family"] for p in found if p["family"] and p["family"] not in FAMILY_LABELS}),
        "units": list(units.items()), "sections": sections if len(sections) > 1 else [],
    })
