from dataclasses import dataclass, field

from core.errors import ApiError


@dataclass(frozen=True)
class Ctx:
    """Who is acting, in which clinic, with which codes (design 5.0).

    Only the decorators and the job runner build one. Services take it and check
    their codes again, including codes that depend on the request body.
    """

    clinic: object
    membership: object
    user_id: int | None
    actor: str  # "member", "system" or "platform"
    codes: frozenset = field(default_factory=frozenset)
    mfa_verified: bool = False
    request_id: str = ""
    ip: str | None = None
    alias: str = ""

    @property
    def clinic_id(self):
        return self.clinic.id

    @property
    def membership_id(self):
        return self.membership.id if self.membership is not None else None

    def can(self, code):
        return code in self.codes

    def require(self, *codes):
        missing = [code for code in codes if code not in self.codes]
        if missing:
            from django.utils.translation import gettext as _

            raise ApiError("forbidden", _("You don't have permission to do this."), 403)

    def require_member(self):
        """Services that read patient data refuse system and platform contexts."""
        if self.actor != "member":
            raise ApiError("forbidden", "Not available to system or platform contexts.", 403)
