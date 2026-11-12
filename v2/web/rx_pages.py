"""Prescription (design E3): the clinic's medicines list and its ready prescriptions (templates), which doctors pick
from when they write a prescription in the visit. Actions go through /api/v1/medicines and
/api/v1/prescription-templates from prescriptions.js; this file only renders.
"""

import json

from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from core.api import page

from . import places
from . import nav
from .http import clinic_page, fragment

CODE = "settings.medicines"
MEDICINE_KEYS = ("id", "section_id", "name", "generic", "strength", "form", "default_dose", "default_note", "form_ar",
                 "default_dose_ar", "default_note_ar", "form_en", "default_dose_en", "default_note_en")
TEMPLATE_KEYS = ("id", "section_id", "name", "name_ar", "name_en", "notes", "notes_ar", "notes_en", "version", "items")


def _section_names(ctx):
    sections = places.sections(ctx)
    return {s["id"]: s["name"] for s in sections} if len(sections) > 1 else {}


def _medicines(ctx):
    from clinical import selectors

    names = _section_names(ctx)
    items = selectors.medicines(ctx)["items"]
    for m in items:
        m["section_name"] = names.get(m["section_id"], "")
        m["json"] = json.dumps({k: m[k] for k in MEDICINE_KEYS}, ensure_ascii=False)
    return items


def _templates(ctx):
    from clinical import selectors

    names = _section_names(ctx)
    items = selectors.templates(ctx)["items"]
    for r in items:
        r["section_name"] = names.get(r["section_id"], "")
        r["json"] = json.dumps({k: r[k] for k in TEMPLATE_KEYS}, ensure_ascii=False)
    return items


@require_GET
@ensure_csrf_cookie
@page(perm=CODE)
@clinic_page
def prescriptions_page(request, ctx):
    if nav.is_manager(ctx):  # the owner manages; doctors write prescriptions (Saged, 2026-11-12)
        return redirect("/overview")
    sections = places.sections(ctx)
    return render(request, "web/prescriptions.html", {
        "ctx": ctx, "nav_active": "prescription", "medicines": _medicines(ctx), "templates": _templates(ctx),
        "sections": sections if len(sections) > 1 else [],
    })


@fragment(perm=CODE)
def medicine_rows(request, ctx):
    """GET /prescriptions/medicines: the list again after a save."""
    return HttpResponse(render_to_string("web/_medicine_rows.html", {"medicines": _medicines(ctx)}, request=request))


@fragment(perm=CODE)
def template_rows(request, ctx):
    """GET /prescriptions/templates: the ready prescriptions again after a save."""
    return HttpResponse(render_to_string("web/_template_rows.html", {"templates": _templates(ctx)}, request=request))
