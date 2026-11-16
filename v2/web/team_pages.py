"""Team & settings (design 5.8, G1, G2, G3): who can do what, the members, their places, and the clinic's names.

The page carries the team as GET /api/v1/team answers it (json_script); team.js draws the table from it and
reads it again after a conflict. Every change goes through /api/v1/team/ and /api/v1/settings/; this file only
renders. Someone with team.manage sees the team; someone with settings.manage sees the settings card.
"""

from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from core.api import page
from core.i18n import pick

from . import places
from .http import clinic_page

CODES = ("team.manage", "settings.manage")


def _sections(ctx):
    """The place's active sections with both names, for the assistants' limits and the settings card."""
    from clinics.models import Section

    found = Section.objects.filter(clinic_id=ctx.clinic_id, is_active=True).order_by("sort", "id")
    return [{"id": s.id, "name": pick(s.name, s.name_en), "name_ar": s.name, "name_en": s.name_en} for s in found]


def _owned_places(ctx):
    """How many active places this owner owns (the Places dialog only helps with more than one)."""
    if not ctx.membership.is_owner:
        return 0
    from directory.models import UserClinic

    return UserClinic.objects.filter(user_id=ctx.user_id, role="owner", is_active=True,
                                     clinic__status="active").count()


def _overview(ctx):
    """Who has what, where (design G1, G2): each active non-owner of this place, the places they work in with their
    role and permission groups there, and the sections they are limited to. Read only.

    The owner sees every place they own: one read per place, on its shard (team.place_transaction). Someone else
    with team.manage sees this place only. A place that can't be read now is marked `unavailable`.
    """
    from django.db import DatabaseError

    from clinics import permissions as perms
    from clinics import team
    from clinics.models import Membership

    def groups(member):
        codes = team.codes_of(member)
        return {key: perms.group_state(codes, key) for key in perms.GROUP_CODES}

    here = list(Membership.objects.filter(clinic_id=ctx.clinic_id, is_active=True).exclude(role="owner")
                .order_by("id")[:team.TEAM_LIMIT])
    section_names = {s["id"]: s["name"] for s in _sections(ctx)}
    clinic = ctx.clinic
    places = [(ctx.clinic_id, pick(clinic.name, clinic.name_en), {m.user_id: m for m in here}, False)]
    if ctx.membership.is_owner and here:
        for clinic_id, entry in team._owned_places(ctx).items():
            if clinic_id == ctx.clinic_id:
                continue
            try:
                with team.place_transaction(ctx, clinic_id, entry.shard):
                    found = {m.user_id: m for m in Membership.objects.filter(
                        clinic_id=clinic_id, is_active=True, user_id__in=[m.user_id for m in here])}
                places.append((clinic_id, pick(entry.name, entry.name_en), found, False))
            except DatabaseError:
                places.append((clinic_id, pick(entry.name, entry.name_en), {}, True))
    people = []
    for m in here:
        rows = []
        for clinic_id, name, members, unavailable in places:
            there = members.get(m.user_id)
            if unavailable:
                rows.append({"clinic_id": clinic_id, "name": name, "unavailable": True})
            elif there is not None:
                rows.append({"clinic_id": clinic_id, "name": name, "current": clinic_id == ctx.clinic_id,
                             "role": there.role, "groups": groups(there)})
        people.append({"member_id": m.id, "places": rows,
                       "sections": [section_names[s] for s in m.sections if s in section_names]})
    return {"people": people, "place_count": len(places)}


@require_GET
@ensure_csrf_cookie
@page()
@clinic_page
def team_page(request, ctx):
    can_team, can_settings = ctx.can("team.manage"), ctx.can("settings.manage")
    if not (can_team or can_settings):
        return render(request, "web/forbidden.html", {"ctx": ctx}, status=403)
    from clinics import team

    sections = _sections(ctx)
    center = places.is_center(ctx.clinic)
    context = {"ctx": ctx, "nav_active": "team", "can_team": can_team, "can_settings": can_settings,
               "sections": sections}
    if can_team:
        context["team"] = {
            **team.team(ctx), "is_owner": ctx.membership.is_owner, "me": ctx.membership_id,
            # Assistants are limited to sections only in a center with more than one (design G1).
            "sections": [{"id": s["id"], "name": s["name"]} for s in sections] if center and len(sections) > 1 else [],
            "places": _owned_places(ctx) > 1,
            "overview": _overview(ctx),
        }
    if can_settings:
        clinic = ctx.clinic
        context["profile"] = {"name": clinic.name, "name_en": clinic.name_en, "city": clinic.city,
                              "phone": clinic.phone}
        from clinics import settings as clinic_settings

        effective = clinic_settings.effective(clinic.settings)
        context["booking"] = {key: effective[key] for key in ("booking_deposit", "booking_deposit_amount")}
    return render(request, "web/team.html", context)
