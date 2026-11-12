"""Team endpoints (design 6.2, G1, G2): validate, check the declared code, call one service."""

import uuid

from django.utils.translation import gettext as _

from core import validate
from core.api import Result, api, by_method
from core.errors import ApiError
from core.text import clean_name, is_valid_phone, normalize_phone

from . import team

USERNAME_PATTERN = r"^[a-z][a-z0-9._-]{2,29}$"


def _member_id(value):
    return validate.path_id(value, team.not_found())


def _version(data):
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ApiError("invalid", _("Send the version you read."), fields={"version": "required"})
    return version


def _names(data, required=False):
    names = {}
    for name, size in team.NAME_FIELDS.items():
        if name in data or (required and name == "display_name"):
            names[name] = clean_name(validate.text(data, name, size, required=required and name == "display_name"))
    return names


def _replay_member(ctx, stored):
    return {"member": team.member(ctx, stored["target_id"])}


@api("GET", perm="team.manage")
def team_view(request, ctx, data):
    return team.team(ctx)


def _clean_new(data):
    import re

    mode = validate.choice(data, "mode", ["new", "invite"])
    username = validate.text(data, "username", 150).lower()
    if not re.match(USERNAME_PATTERN, username):
        message = _("A username has 3 to 30 English letters, digits, dots, dashes or underscores, and starts with a letter.")
        raise ApiError("invalid", message, fields={"username": "format"})
    values = {"mode": mode, "username": username, "role": validate.choice(data, "role", ["doctor", "assistant"]),
              **_names(data, required=True), "sections": data.get("sections")}
    if mode == "new":
        values["full_name"] = clean_name(validate.text(data, "full_name", 120))
        phone = normalize_phone(validate.text(data, "phone", 20, required=False))
        if phone and not is_valid_phone(phone):
            raise ApiError("invalid", _("Enter a valid mobile number."), fields={"phone": "phone"})
        values["phone"] = phone
        values["language"] = validate.choice({"language": data.get("language") or "ar"}, "language", ["ar", "en"])
    return values


@api("POST", perm="team.manage", replay=team.replay_add)
def add_member_view(request, ctx, data):
    """{mode: "new", username, full_name, phone?, role, display_name, display_name_en?, title?, sections?}
    or {mode: "invite", username, role, display_name, ...} (design G1)."""
    values = _clean_new(data)
    if values["mode"] == "invite":
        team.invite(ctx, values)
        return Result({"invited": True}, status=202, target_type="invitation")
    key = uuid.UUID(request.headers["Idempotency-Key"])
    member, account = team.add_member(ctx, key, values)
    return Result({"member": member, "account": account}, status=201, target_type="membership", target_id=member["id"])


team_members_endpoint = by_method(POST=add_member_view)


@api("PATCH", perm="team.manage", replay=_replay_member)
def member_update_view(request, ctx, data, member_id):
    """{version, role?, is_active?, sections?, display_name?, display_name_en?, title?}."""
    member_id = _member_id(member_id)
    unknown = set(data) - {"version", "role", "is_active", "sections", *team.NAME_FIELDS}
    if unknown:
        raise ApiError("invalid", "Unknown fields: " + ", ".join(sorted(unknown)),
                       fields={name: "unknown" for name in unknown})
    changes = _names(data)
    if "display_name" in changes and not changes["display_name"]:
        raise ApiError("invalid", "display_name is required", fields={"display_name": "required"})
    if "role" in data:
        changes["role"] = validate.choice(data, "role", ["doctor", "assistant"])
    if "is_active" in data:
        changes["is_active"] = validate.boolean(data, "is_active")
    if "sections" in data:
        changes["sections"] = data["sections"]
    member = team.update_member(ctx, member_id, _version(data), changes)
    return Result({"member": member}, target_type="membership", target_id=member_id)


@api("PUT", perm="team.manage", replay=_replay_member)
def permissions_view(request, ctx, data, member_id):
    """{version, groups?: {key: bool}, codes?: {code: bool}}: groups first, then single codes (design G1)."""
    member_id = _member_id(member_id)
    groups, codes = data.get("groups") or {}, data.get("codes") or {}
    for name, value in (("groups", groups), ("codes", codes)):
        if not isinstance(value, dict) or not all(isinstance(v, bool) for v in value.values()):
            raise ApiError("invalid", f"{name} maps names to true or false", fields={name: "object"})
    if not groups and not codes:
        raise ApiError("invalid", _("Send the changes."), fields={"codes": "required"})
    member = team.set_permissions(ctx, member_id, _version(data), groups, codes)
    return Result({"member": member}, target_type="membership", target_id=member_id)


def _replay_password(ctx, stored):
    return {"account": None, "replayed": True}


@api("POST", perm="team.manage", replay=_replay_password)
def password_view(request, ctx, data, member_id):
    """A new temporary password for an account this place created and that has never signed in (B6)."""
    account = team.reissue_password(ctx, _member_id(member_id))
    return Result({"account": account}, target_type="membership", target_id=member_id)


@api("GET", perm="team.manage")
def places_view(request, ctx, data, member_id):
    return team.places(ctx, _member_id(member_id))


def _replay_places(ctx, stored):
    return team.places(ctx, stored["target_id"])


@api("PUT", perm="team.manage", replay=_replay_places)
def places_update_view(request, ctx, data, member_id):
    """{places: [{clinic_id, role?}], copy: bool} (design G2)."""
    member_id = _member_id(member_id)
    wanted, copy = team.clean_places(data)
    return Result(team.set_places(ctx, member_id, wanted, copy), target_type="membership", target_id=member_id)


member_places_endpoint = by_method(GET=places_view, PUT=places_update_view)


# --- The invitee (directory, signed in) -------------------------------------------------------------------------


@api("GET", member=False)
def my_invitations_view(request, data):
    if not request.user.is_authenticated:
        raise ApiError("signed_out", _("Please sign in."), 401)
    return team.my_invitations(request.user)


def _replay_accept(user, stored):
    from directory.models import Invitation

    found = Invitation.objects.filter(id=stored["target_id"], user=user).first()
    return {"clinic_id": found.clinic_id if found else None, "membership_id": found.membership_id if found else None}


@api("POST", member=False, replay=_replay_accept)
def accept_invitation_view(request, data, invitation_id):
    invitation_id = validate.path_id(invitation_id, ApiError("not_found", _("This invitation wasn't found or has expired."), 404))
    body = team.accept_invitation(request.user, invitation_id)
    return Result(body, target_type="invitation", target_id=invitation_id)
