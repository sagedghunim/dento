"""The team: members, the "Who can do what" table, adding people and their places (design 5.8, 5.12, G1, G2).

Lock order (design 5.0, G1): request key -> the target membership, by a conditional UPDATE on its version. Steps in
other places (G2) each run alone: on another shard in its own transaction, on this shard in a savepoint with
app.clinic_id switched and restored; every step writes absolute values, so a retry changes nothing that is right.
"""

import datetime as dt
from contextlib import contextmanager

from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from audit.services import record_change
from core import sql
from core.context import Ctx
from core.errors import ApiError
from core.i18n import pick
from core.routing import clinic_scope, clinic_transaction, set_clinic_setting

from . import permissions as perms
from .models import Clinic, Membership, Section

TEAM_LIMIT = 200  # a place's whole team
MAX_SECTIONS = 20
MAX_PLACES = 50  # one owner's places
INVITATION_DAYS = 7
NAME_FIELDS = {"display_name": 120, "display_name_en": 120, "title": 120}


def not_found():
    return ApiError("not_found", _("This team member wasn't found."), 404)


def _stale(current):
    return ApiError("stale", _("Someone changed this person meanwhile. Reload and try again."), 409,
                    fields={"version": "changed"}, extra={"version": current})


def refused(error):
    """A GrantRefused as an API answer (403 for a rule, 400 for an unknown name), in the user's language."""
    messages = {
        "forbidden": _("You don't have permission to do this."),
        "not_self": _("You can't change your own role, permissions or status."),
        "owner_fixed": _("The owner's role and permissions can't be changed."),
        "owner_only_target": _("Only the owner can change someone who manages the team."),
        "owner_only_code": _("Only the owner gives or removes this permission."),
        "not_held": _("You can only give or remove permissions you hold yourself."),
    }
    if error.code in messages:
        return ApiError(error.code, messages[error.code], 403, extra={"codes": error.detail} if error.detail else None)
    return ApiError("invalid", f"{error.code}: {error.detail}", 400, fields={error.code: error.detail})


def _check(actor, target, role, granted, revoked):
    try:
        return perms.check_change(actor, target, role, granted, revoked)
    except perms.GrantRefused as error:
        raise refused(error)


# --- Reading ---------------------------------------------------------------------------------------------------


def codes_of(member):
    if not member.is_active:
        return frozenset()
    return perms.effective_codes(member.role, member.granted, member.revoked)


def member_payload(member, actor_membership=None):
    codes = codes_of(member)
    body = {
        "id": member.id, "name": pick(member.display_name, member.display_name_en),
        "display_name": member.display_name, "display_name_en": member.display_name_en, "title": member.title,
        "role": member.role, "is_active": member.is_active, "sections": list(member.sections), "version": member.version,
        "codes": sorted(codes), "groups": {key: perms.group_state(codes, key) for key in perms.GROUP_CODES},
        "hours": {"days": list(member.work_days or []),
                  "starts": member.work_starts.strftime("%H:%M") if member.work_starts else "",
                  "ends": member.work_ends.strftime("%H:%M") if member.work_ends else ""},
        "hours_text": hours_text(member),
    }
    if actor_membership is not None:
        body["is_me"] = member.id == actor_membership.id
        body["editable"] = _editable(actor_membership, member)
    return body


WEEK = (5, 6, 0, 1, 2, 3, 4)  # Saturday first, as in Egypt; Python's Monday is 0


def hours_text(member):
    """"Saturday – Thursday · 8:00 AM – 4:00 PM", or "" when no hours are set (Saged, 2026-11-22)."""
    from django.utils.dates import WEEKDAYS

    from web.format import clock_text

    days = [d for d in WEEK if d in (member.work_days or [])]
    parts = []
    if len(days) == 7:
        parts.append(_("Every day"))
    elif days:
        at = [WEEK.index(d) for d in days]
        if len(days) >= 3 and at == list(range(at[0], at[0] + len(at))):  # one run of days
            parts.append(f"{WEEKDAYS[days[0]]} – {WEEKDAYS[days[-1]]}")
        else:
            parts.append("، ".join(str(WEEKDAYS[d]) for d in days))
    if member.work_starts and member.work_ends:
        parts.append(f"{clock_text(member.work_starts)} – {clock_text(member.work_ends)}")
    return " · ".join(parts)


def set_hours(ctx, member_id, version, days, starts, ends):
    """PUT /team/members/{id}/hours: the working days and hours the owner writes down (Saged, 2026-11-22). They are
    shown on Team only; signing in is never limited by them. No days and no times clears them."""
    target = _target(ctx, member_id)
    if target.version != version:
        raise _stale(target.version)
    before = {"days": list(target.work_days or []), "starts": target.work_starts, "ends": target.work_ends}
    values = {"work_days": days, "work_starts": starts, "work_ends": ends}
    if (days, starts, ends) == (before["days"], before["starts"], before["ends"]):
        return member_payload(target, ctx.membership)
    target.version = _write(ctx, target.id, version, values)
    target.work_days, target.work_starts, target.work_ends = days, starts, ends
    record_change(ctx, "team.hours", "membership", target.id,
                  before={"days": before["days"], "starts": str(before["starts"] or ""), "ends": str(before["ends"] or "")},
                  after={"days": days, "starts": str(starts or ""), "ends": str(ends or "")})
    return member_payload(target, ctx.membership)


def _editable(actor, target):
    """Whether the table should offer switches for this person (the server checks every change again)."""
    try:
        perms.check_change(actor, target, target.role, target.granted, target.revoked)
    except perms.GrantRefused:
        return False
    return True


def groups_payload():
    return [{"key": key, "label": str(label), "description": str(description),
             "codes": [{"code": code, "label": str(perms.CODES[code]), "owner_only": code in perms.OWNER_ONLY}
                       for code in codes]}
            for key, label, description, codes in perms.GROUPS]


def team(ctx):
    """GET /team: rows are the permission groups, columns the members (one query)."""
    members = list(Membership.objects.filter(clinic_id=ctx.clinic_id).order_by("-is_active", "id")[:TEAM_LIMIT])
    return {"groups": groups_payload(), "roles": ["owner", "doctor", "assistant"],
            "members": [member_payload(m, ctx.membership) for m in members]}


def _target(ctx, member_id):
    found = Membership.objects.filter(clinic_id=ctx.clinic_id, id=member_id).first()
    if found is None:
        raise not_found()
    return found


def member(ctx, member_id):
    return member_payload(_target(ctx, member_id), ctx.membership)


# --- Writing one member ----------------------------------------------------------------------------------------


def _write(ctx, member_id, version, values):
    """One conditional UPDATE on the version; 409 stale when someone else wrote first (design 5.0)."""
    assignments = ", ".join(f"{name} = %s" for name in values)
    found = sql.row(ctx, f'UPDATE "clinics_membership" SET {assignments}, version = version + 1 '
                         "WHERE clinic_id = %s AND id = %s AND version = %s RETURNING version",
                    [*values.values(), ctx.clinic_id, member_id, version])
    if found is None:
        current = Membership.objects.filter(clinic_id=ctx.clinic_id, id=member_id).values_list("version", flat=True).first()
        if current is None:
            raise not_found()
        raise _stale(current)
    return found[0]


def clean_sections(ctx, value, role):
    """Section ids for an assistant (design G1): distinct, at most 20, each an active section of this place."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SECTIONS:
        raise ApiError("invalid", _("Pick up to 20 sections."), fields={"sections": "list"})
    ids = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ApiError("invalid", "sections must be section ids", fields={"sections": "id"})
        if item not in ids:
            ids.append(item)
    if ids and role != "assistant":
        raise ApiError("invalid", _("Only assistants can be limited to some sections."), fields={"sections": "role"})
    if ids:
        found = Section.objects.filter(clinic_id=ctx.clinic_id, id__in=ids, is_active=True).count()
        if found != len(ids):
            raise ApiError("not_found", _("This section wasn't found."), 404, fields={"sections": "not_found"})
    return sorted(ids)


def _snapshot(member):
    return {"role": member.role, "is_active": member.is_active, "sections": list(member.sections),
            "granted": sorted(member.granted), "revoked": sorted(member.revoked), "display_name": member.display_name,
            "display_name_en": member.display_name_en, "title": member.title}


def _sync_link(user_id, clinic_id, membership_id, role, is_active):
    """The directory's copy of a membership, for the picker (design 3.3). The shard stays the authority."""
    from directory.models import UserClinic

    UserClinic.objects.update_or_create(user_id=user_id, clinic_id=clinic_id,
                                        defaults={"membership_id": membership_id, "role": role, "is_active": is_active})


def _sync_after_commit(ctx, member):
    user_id, clinic_id, member_id, role, active = member.user_id, ctx.clinic_id, member.id, member.role, member.is_active
    transaction.on_commit(lambda: _sync_link(user_id, clinic_id, member_id, role, active), using=ctx.alias)


def update_member(ctx, member_id, version, changes):
    """PATCH /team/members/{id}: role, status, sections and names, with the version the screen read (design G1).

    `changes` is validated by the view (types and lengths). A person may change only their own names.
    """
    target = _target(ctx, member_id)
    if target.version != version:
        raise _stale(target.version)
    before = _snapshot(target)
    values = {name: changes[name] for name in NAME_FIELDS if name in changes}
    rules_touched = {"role", "is_active", "sections"} & set(changes)
    if rules_touched or target.id != ctx.membership_id:
        role = changes.get("role", target.role)
        granted, revoked = (target.granted, target.revoked) if role == target.role else ([], [])
        active = changes.get("is_active", target.is_active)
        # Deactivating removes every code the person holds, so it is checked as that (design G1).
        _check(ctx.membership, target, role, granted if active else [], revoked if active else sorted(perms.ALL))
        if active and not target.is_active and not ctx.membership.is_owner:
            # Reactivating gives back every code the person would hold: checked as adding them (design 5.8).
            gained = perms.effective_codes(role, granted, revoked)
            if gained & perms.OWNER_ONLY:
                raise refused(perms.GrantRefused("owner_only_code", ", ".join(sorted(gained & perms.OWNER_ONLY))))
            if not gained <= ctx.codes:
                raise refused(perms.GrantRefused("not_held", ", ".join(sorted(gained - ctx.codes))))
        if role != target.role:
            values.update(role=role, granted=list(granted), revoked=list(revoked))
        if active != target.is_active:
            values["is_active"] = active
        sections = clean_sections(ctx, changes["sections"], role) if "sections" in changes else (
            list(target.sections) if role == "assistant" else [])
        if sections != list(target.sections):
            values["sections"] = sections
    if not values:
        return member_payload(target, ctx.membership)
    target.version = _write(ctx, target.id, version, values)
    for name, value in values.items():
        setattr(target, name, value)
    after = _snapshot(target)
    record_change(ctx, "team.change", "membership", target.id,
                  before={k: v for k, v in before.items() if before[k] != after[k]},
                  after={k: v for k, v in after.items() if before[k] != after[k]})
    if {"role", "is_active"} & set(values):
        _sync_after_commit(ctx, target)
    return member_payload(target, ctx.membership)


def set_permissions(ctx, member_id, version, groups, codes):
    """PUT /team/members/{id}/permissions: switch groups, then single codes (design G1)."""
    target = _target(ctx, member_id)
    if target.version != version:
        raise _stale(target.version)
    if not target.is_active:
        raise ApiError("inactive", _("Activate this person first."), 409)
    try:
        wanted = perms.wanted_codes(codes_of(target), groups, codes)
    except perms.GrantRefused as error:
        raise refused(error)
    granted, revoked = perms.lists_for(target.role, wanted)
    before, after = _check(ctx.membership, target, target.role, granted, revoked)
    if before == after:
        return member_payload(target, ctx.membership)
    target.version = _write(ctx, target.id, version, {"granted": granted, "revoked": revoked})
    target.granted, target.revoked = granted, revoked
    record_change(ctx, "team.permissions", "membership", target.id,
                  before={"added": [], "removed": []}, after={"added": sorted(after - before),
                                                              "removed": sorted(before - after)})
    return member_payload(target, ctx.membership)


# --- Adding a member (design 5.12, B6) --------------------------------------------------------------------------


def _username_taken():
    return ApiError("username_taken", _("That username is taken."), 409, fields={"username": "taken"})


def _account(ctx, key, values):
    """The directory account for "new": created here, or the one this same request created before (B6).

    Any other existing username is 409, as public sign-up answers (design D2, G1). Returns (user, password or None).
    """
    from accounts.models import User
    from directory.flows import TEMP_PASSWORD_HOURS, temporary_password

    found = User.objects.filter(username=values["username"]).first()
    if found is not None:
        if (found.created_request_key == key and found.created_for_clinic == ctx.clinic_id
                and found.must_change_password and found.last_login is None):
            return found, None  # a retry of the same request: reused, and no password is shown again
        raise _username_taken()
    password = temporary_password()
    try:
        with transaction.atomic(using="default"):
            user = User.objects.create_user(
                values["username"], password=password, full_name=values["full_name"], phone=values["phone"] or None,
                language=values.get("language", "ar"), must_change_password=True,
                temp_password_expires_at=timezone.now() + dt.timedelta(hours=TEMP_PASSWORD_HOURS),
                created_for_clinic=ctx.clinic_id, created_request_key=key)
    except IntegrityError:
        if User.objects.filter(username=values["username"]).exists():
            raise _username_taken()
        raise ApiError("phone_taken", _("That mobile number already has an account."), 409, fields={"phone": "taken"})
    return user, password


def _insert_member(ctx, clinic_id, user_id, role, names, granted=(), revoked=(), sections=()):
    """Insert a membership unless the account already has one in that place. Returns (id, inserted)."""
    found = sql.row(ctx, """
        INSERT INTO "clinics_membership" (clinic_id, user_id, role, display_name, display_name_en, title, is_active,
                                          granted, revoked, sections, version, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, true, %s, %s, %s::bigint[], 1, %s)
        ON CONFLICT (clinic_id, user_id) DO NOTHING RETURNING id""",
                    [clinic_id, user_id, role, names["display_name"], names.get("display_name_en", ""),
                     names.get("title", ""), list(granted), list(revoked), list(sections), timezone.now()])
    if found:
        return found[0], True
    existing = sql.row(ctx, 'SELECT id FROM "clinics_membership" WHERE clinic_id = %s AND user_id = %s',
                       [clinic_id, user_id])
    return existing[0], False


def add_member(ctx, key, values):
    """POST /team/members, mode "new": the account in the directory, then the membership, then the picker row.

    Returns (member payload, account body). A retry of the same request finds its account and membership.
    """
    try:
        perms.check_add(ctx.membership, values["role"])
    except perms.GrantRefused as error:
        raise refused(error)
    sections = clean_sections(ctx, values.get("sections"), values["role"])
    user, password = _account(ctx, key, values)
    member_id, inserted = _insert_member(ctx, ctx.clinic_id, user.id, values["role"], values, sections=sections)
    target = _target(ctx, member_id)
    if inserted:
        record_change(ctx, "team.add", "membership", member_id, after=_snapshot(target))
    _sync_after_commit(ctx, target)
    return member_payload(target, ctx.membership), account_body(user, password)


def account_body(user, password):
    return {"username": user.username, "temporary_password": password,
            "expires_at": user.temp_password_expires_at.isoformat() if password and user.temp_password_expires_at else None,
            # A replay shows no password; the screen offers to issue a new one (B6).
            "can_reissue": password is None and _never_signed_in(user)}


def _never_signed_in(user):
    return user.must_change_password and user.last_login is None


def replay_add(ctx, stored):
    """A repeat of an add: the member as it is now, and no password (B6). Repairs a lost picker row."""
    from accounts.models import User

    target = Membership.objects.filter(clinic_id=ctx.clinic_id, id=stored["target_id"]).first()
    if target is None:
        return {"member": None, "account": None}
    _sync_link(target.user_id, ctx.clinic_id, target.id, target.role, target.is_active)
    user = User.objects.filter(id=target.user_id).first()
    return {"member": member_payload(target, ctx.membership), "account": account_body(user, None) if user else None}


def reissue_password(ctx, member_id):
    """A new temporary password, only for an account this place created that has never signed in (B6)."""
    from accounts.models import User
    from directory.flows import TEMP_PASSWORD_HOURS, temporary_password

    target = _target(ctx, member_id)
    _check(ctx.membership, target, target.role, target.granted, target.revoked)
    user = User.objects.filter(id=target.user_id).first()
    if user is None or user.created_for_clinic != ctx.clinic_id or not _never_signed_in(user):
        raise ApiError("not_eligible", _("This person has already signed in. They change their own password."), 409)
    password = temporary_password()
    user.set_password(password)
    user.temp_password_expires_at = timezone.now() + dt.timedelta(hours=TEMP_PASSWORD_HOURS)
    user.save(update_fields=["password", "temp_password_expires_at"])
    record_change(ctx, "team.password", "membership", target.id)
    return account_body(user, password)


# --- Invitations (design 5.8, G1) ------------------------------------------------------------------------------


def invite(ctx, values):
    """POST /team/members, mode "invite": the same answer whether or not the username exists (design 5.8)."""
    from accounts.models import User
    from directory.models import Invitation

    try:
        perms.check_add(ctx.membership, values["role"])
    except perms.GrantRefused as error:
        raise refused(error)
    user = User.objects.filter(username=values["username"], is_active=True, is_platform_admin=False).first()
    record_change(ctx, "team.invite", "membership", None, after={"username": values["username"], "role": values["role"]})
    if user is None or Membership.objects.filter(clinic_id=ctx.clinic_id, user_id=user.id).exists():
        return
    with transaction.atomic(using="default"):
        Invitation.objects.filter(clinic_id=ctx.clinic_id, user=user, accepted_at__isnull=True).delete()
        Invitation.objects.create(
            clinic_id=ctx.clinic_id, user=user, role=values["role"], display_name=values["display_name"],
            display_name_en=values.get("display_name_en", ""), title=values.get("title", ""),
            invited_by_membership_id=ctx.membership_id,
            expires_at=timezone.now() + dt.timedelta(days=INVITATION_DAYS))


def my_invitations(user):
    from directory.models import Invitation

    rows = (Invitation.objects.filter(user=user, accepted_at__isnull=True, expires_at__gt=timezone.now(),
                                      clinic__status="active").select_related("clinic").order_by("id")[:50])
    return {"items": [{"id": i.id, "clinic": {"id": i.clinic_id, "name": pick(i.clinic.name, i.clinic.name_en),
                                              "kind": i.clinic.kind},
                       "role": i.role, "expires_at": i.expires_at.isoformat()} for i in rows]}


def accept_invitation(user, invitation_id):
    """The invitee accepts: the shard membership, then the picker row, then the invitation is closed.

    Each step finds its row if a previous try made it, so a retry finishes the job.
    """
    from directory.models import Invitation

    found = Invitation.objects.filter(id=invitation_id, user=user).select_related("clinic").first()
    if found is None or (found.accepted_at is None and found.expires_at <= timezone.now()):
        raise ApiError("not_found", _("This invitation wasn't found or has expired."), 404)
    entry = found.clinic
    with clinic_transaction(entry.id, entry.shard):
        clinic = Clinic.objects.filter(id=entry.id).first()
        if clinic is None or not clinic.owned or clinic.status != "active":
            raise ApiError("clinic_moved", _("The clinic is being moved. Try again in a moment."), 503, retry_after=2)
        member_id, inserted = _insert_member(_shard_ctx(clinic, None, user.id, entry.shard), entry.id, user.id,
                                             found.role, {"display_name": found.display_name,
                                                          "display_name_en": found.display_name_en,
                                                          "title": found.title})
        if inserted:
            target = Membership.objects.get(clinic_id=entry.id, id=member_id)
            record_change(_shard_ctx(clinic, target, user.id, entry.shard), "team.join", "membership", member_id,
                          after=_snapshot(target))
        role, active = Membership.objects.filter(clinic_id=entry.id, id=member_id).values_list("role", "is_active").get()
    _sync_link(user.id, entry.id, member_id, role, active)
    Invitation.objects.filter(id=found.id, accepted_at__isnull=True).update(accepted_at=timezone.now(),
                                                                         membership_id=member_id)
    return {"clinic_id": entry.id, "membership_id": member_id}


# --- Places (design G2) ----------------------------------------------------------------------------------------


def _owner_only(ctx):
    if not ctx.membership.is_owner:
        raise ApiError("owner_only", _("Only the owner manages a person's places."), 403)


def _owned_places(ctx):
    from directory.models import UserClinic

    links = (UserClinic.objects.filter(user_id=ctx.user_id, role="owner", is_active=True, clinic__status="active")
             .select_related("clinic").order_by("clinic_id")[:MAX_PLACES])
    return {link.clinic_id: link.clinic for link in links}


def places(ctx, member_id):
    """GET /team/members/{id}/places: the owner's places and this person's role in each, from the directory."""
    from directory.models import UserClinic

    _owner_only(ctx)
    target = _target(ctx, member_id)
    owned = _owned_places(ctx)
    links = {link.clinic_id: link for link in UserClinic.objects.filter(user_id=target.user_id, clinic_id__in=list(owned))}
    return {"member_id": target.id, "items": [
        {"clinic_id": clinic_id, "name": pick(entry.name, entry.name_en), "kind": entry.kind,
         "current": clinic_id == ctx.clinic_id,
         "member": ({"membership_id": links[clinic_id].membership_id, "role": links[clinic_id].role,
                     "is_active": links[clinic_id].is_active} if clinic_id in links else None)}
        for clinic_id, entry in owned.items()]}


def _shard_ctx(clinic, membership, user_id, alias, base=None):
    return Ctx(clinic=clinic, membership=membership, user_id=user_id, actor="member", codes=perms.ALL,
               request_id=base.request_id if base else "", ip=base.ip if base else None, alias=alias)


@contextmanager
def place_transaction(ctx, clinic_id, alias):
    """Work on another place: its own transaction on another shard; on this request's shard a savepoint with
    app.clinic_id switched, put back afterwards even when the savepoint rolls back (design G2)."""
    if alias != ctx.alias:
        with clinic_transaction(clinic_id, alias):
            yield
        return
    try:
        with clinic_scope(clinic_id, alias), transaction.atomic(using=alias):
            set_clinic_setting(alias, clinic_id)
            yield
    finally:
        set_clinic_setting(alias, ctx.clinic_id)


def _apply_place(ctx, entry, source, wanted):
    """Make one place match `wanted` ({"active", "role", "granted", "revoked"}). Returns (status, membership)."""
    clinic = Clinic.objects.filter(id=entry.id).first()
    if clinic is None or not clinic.owned or clinic.status != "active":
        return "unavailable", None
    owner = Membership.objects.filter(clinic_id=entry.id, user_id=ctx.user_id, role="owner").first()
    if owner is None:
        return "not_owner", None  # the directory's copy was stale: the shard is the authority
    place_ctx = _shard_ctx(clinic, owner, ctx.user_id, entry.shard, ctx)
    current = Membership.objects.filter(clinic_id=entry.id, user_id=source.user_id).first()
    if current is not None and current.role == "owner":
        return "owner", current
    if not wanted["active"]:
        if current is None or not current.is_active:
            return "unchanged", current
        sql.run(place_ctx, 'UPDATE "clinics_membership" SET is_active = false, version = version + 1 '
                           "WHERE clinic_id = %s AND id = %s", [entry.id, current.id])
        record_change(place_ctx, "team.place", "membership", current.id, before={"is_active": True},
                      after={"is_active": False})
        current.is_active = False
        return "removed", current
    names = {"display_name": source.display_name, "display_name_en": source.display_name_en, "title": source.title}
    if current is None:
        member_id, _inserted = _insert_member(place_ctx, entry.id, source.user_id, wanted["role"], names,
                                              wanted["granted"], wanted["revoked"])
        current = Membership.objects.get(clinic_id=entry.id, id=member_id)
        record_change(place_ctx, "team.place", "membership", member_id, after=_snapshot(current))
        return "added", current
    values = {"is_active": True}
    if wanted["role"] != current.role or wanted["copy"]:
        values.update(role=wanted["role"], granted=list(wanted["granted"]), revoked=list(wanted["revoked"]),
                      sections=[] if wanted["role"] != "assistant" or wanted["role"] != current.role
                      else list(current.sections))
    changed = {k: v for k, v in values.items() if getattr(current, k) != v and not (
        k in ("granted", "revoked", "sections") and sorted(getattr(current, k)) == sorted(v))}
    if not changed:
        return "unchanged", current
    before = _snapshot(current)
    assignments = ", ".join(f"{name} = %s" for name in changed)
    sql.run(place_ctx, f'UPDATE "clinics_membership" SET {assignments}, version = version + 1 '
                       "WHERE clinic_id = %s AND id = %s", [*changed.values(), entry.id, current.id])
    for name, value in changed.items():
        setattr(current, name, value)
    record_change(place_ctx, "team.place", "membership", current.id,
                  before={k: before[k] for k in changed}, after={k: _snapshot(current)[k] for k in changed})
    return "updated", current


def clean_places(data):
    """[{clinic_id, role?}] (or bare ids) and `copy`."""
    items = data.get("places")
    if not isinstance(items, list) or len(items) > MAX_PLACES:
        raise ApiError("invalid", "places must be a list", fields={"places": "list"})
    wanted = {}
    for index, item in enumerate(items):
        if isinstance(item, int) and not isinstance(item, bool):
            item = {"clinic_id": item}
        if not isinstance(item, dict):
            raise ApiError("invalid", "each place is an object", fields={f"places.{index}": "object"})
        clinic_id = item.get("clinic_id")
        if isinstance(clinic_id, bool) or not isinstance(clinic_id, int) or clinic_id < 1:
            raise ApiError("invalid", "clinic_id must be an id", fields={f"places.{index}.clinic_id": "id"})
        role = item.get("role")
        if role is not None and role not in ("doctor", "assistant"):
            raise ApiError("invalid", "role must be doctor or assistant", fields={f"places.{index}.role": "choice"})
        wanted[clinic_id] = role
    copy = data.get("copy", False)
    if not isinstance(copy, bool):
        raise ApiError("invalid", "copy must be true or false", fields={"copy": "boolean"})
    return wanted, copy


def set_places(ctx, member_id, wanted, copy):
    """PUT /team/members/{id}/places: tick the places this person works in, for the same account (design G2).

    The current place is left alone (PATCH changes it). Any failed place makes the whole answer 503
    `places_incomplete`, so this request's key is freed and the client resends it; finished places are unchanged then.
    """
    _owner_only(ctx)
    source = _target(ctx, member_id)
    if source.role == "owner":
        raise refused(perms.GrantRefused("owner_fixed"))
    owned = _owned_places(ctx)
    unknown = set(wanted) - set(owned)
    if unknown:
        raise ApiError("not_found", _("This place wasn't found."), 404, fields={"places": "not_found"})
    results = []
    for clinic_id, entry in owned.items():
        if clinic_id == ctx.clinic_id:
            continue
        role = wanted.get(clinic_id) or source.role
        lists = (source.granted, source.revoked) if copy and role == source.role else ([], [])
        goal = {"active": clinic_id in wanted, "role": role, "granted": sorted(lists[0]), "revoked": sorted(lists[1]),
                "copy": copy}
        try:
            with place_transaction(ctx, clinic_id, entry.shard):
                status, member = _apply_place(ctx, entry, source, goal)
        except DatabaseError:
            status, member = "failed", None
        if member is not None and status in ("added", "updated", "removed"):
            if entry.shard == ctx.alias:
                user_id, mid, mrole, active = member.user_id, member.id, member.role, member.is_active
                transaction.on_commit(lambda c=clinic_id, u=user_id, m=mid, r=mrole, a=active: _sync_link(u, c, m, r, a),
                                      using=ctx.alias)
            else:
                try:
                    _sync_link(member.user_id, clinic_id, member.id, member.role, member.is_active)
                except DatabaseError:
                    status = "failed"
        results.append({"clinic_id": clinic_id, "name": pick(entry.name, entry.name_en), "status": status,
                        "membership_id": member.id if member else None})
    if any(r["status"] in ("failed", "unavailable") for r in results):
        raise ApiError("places_incomplete", _("Some places couldn't be updated now. Trying again."), 503,
                       retry_after=2, extra={"places": results})
    record_change(ctx, "team.places", "membership", source.id,
                  after={"places": sorted(wanted), "copy": copy})
    return {"member_id": source.id, "places": results}
