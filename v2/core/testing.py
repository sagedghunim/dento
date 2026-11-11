"""Test helpers: clinics on both shards, signed-in clients, and the SQL-shape check (design 10, T13)."""

import json
import re
import uuid

from django.db import connections
from django.test import Client, TestCase
from django.utils import timezone

from accounts import crypto, totp
from accounts.models import User
from core.api import SESSION_ACTIVE, SESSION_MFA
from core.routing import DIRECTORY_APPS
from directory.flows import add_member, create_clinic

_TENANT_TABLE = re.compile(r'(?:FROM|UPDATE|JOIN)\s+"((?:clinics|patients|scheduling|billing|clinical|inventory|finance|consults|summaries|audit|core)_[a-z_]+)"', re.I)


class ShardSQLCheck:
    """Fails any SELECT, UPDATE or DELETE on a tenant table that doesn't constrain clinic_id (design 10, T13).

    The root table of each statement must be filtered on clinic_id; joins along composite
    foreign keys needn't repeat it. This check is a pattern match, not a parser: it looks for
    a clinic_id predicate (or clinics_clinic's own id) anywhere in the WHERE clause.
    """

    def __init__(self):
        self.violations = []

    def __call__(self, execute, sql, params, many, context):
        text = sql if isinstance(sql, str) else str(sql)
        head = text.lstrip()[:6].upper()
        if head in ("SELECT", "UPDATE", "DELETE") and _TENANT_TABLE.search(text):
            where = text.upper().split(" WHERE ", 1)
            clause = where[1] if len(where) > 1 else ""
            ok = '"CLINIC_ID"' in clause or "CLINIC_ID =" in clause or '"CLINICS_CLINIC"."ID"' in clause
            if not ok and "PG_" not in text.upper():
                self.violations.append(text)
        return execute(sql, params, many, context)


class ShardTestCase(TestCase):
    databases = "__all__"
    sql_check = True

    def setUp(self):
        super().setUp()
        self._checks = []
        if self.sql_check:
            from django.conf import settings

            for alias in settings.SHARDS:
                check = ShardSQLCheck()
                wrapper = connections[alias].execute_wrapper(check)
                wrapper.__enter__()
                self.addCleanup(wrapper.__exit__, None, None, None)
                self._checks.append(check)

    def tearDown(self):
        violations = [sql for check in self._checks for sql in check.violations]
        super().tearDown()
        if violations:
            self.fail("Tenant statements without a clinic_id filter:\n" + "\n".join(violations[:5]))

    # --- fixtures ---

    @staticmethod
    def user(username, name=None, *, with_totp=False, **fields):
        user = User.objects.create_user(username, password="pass-word-123", full_name=name or username, **fields)
        if with_totp:
            enable_totp(user)
        return user

    def clinic(self, prefix, shard="shard_1", specialty="dental", sections=None):
        """A clinic with an owner (two-step sign-in on), a doctor and an assistant.

        With `sections` ([{"specialty": ..., "name": ...}, ...]) it is a center with those sections.
        """
        owner = self.user(f"{prefix}_owner", with_totp=True)
        entry = create_clinic(name=f"{prefix} clinic", specialty_code=specialty, owner=owner, shard=shard,
                              kind="center" if sections else "clinic", sections=sections)
        doctor = self.user(f"{prefix}_doctor")
        assistant = self.user(f"{prefix}_assistant")
        from clinics.models import Membership
        from core.routing import clinic_transaction

        add_member(entry, doctor, "doctor", "Doctor")
        add_member(entry, assistant, "assistant", "Assistant")
        with clinic_transaction(entry.id, entry.shard):
            members = {m.user_id: m for m in Membership.objects.filter(clinic_id=entry.id)}
        return Fixture(entry, {"owner": (owner, members[owner.id]), "doctor": (doctor, members[doctor.id]),
                               "assistant": (assistant, members[assistant.id])})

    @staticmethod
    def client_for(user, entry, membership, mfa=True):
        client = Client()
        client.force_login(user)
        session = client.session
        session[SESSION_ACTIVE] = {"clinic_id": entry.id, "membership_id": membership.id}
        session[SESSION_MFA] = mfa
        session.save()
        client.membership_id = membership.id
        return client


class Fixture:
    def __init__(self, entry, people):
        self.entry = entry
        self.people = people

    @property
    def id(self):
        return self.entry.id

    @property
    def shard(self):
        return self.entry.shard

    def user(self, role):
        return self.people[role][0]

    def membership(self, role):
        return self.people[role][1]

    def client(self, role, mfa=True):
        user, membership = self.people[role]
        return ShardTestCase.client_for(user, self.entry, membership, mfa=mfa)


def enable_totp(user):
    secret = totp.new_secret()
    user.totp_secret = crypto.encrypt(secret)
    user.totp_confirmed_at = timezone.now()
    user.save(update_fields=["totp_secret", "totp_confirmed_at"])
    user.plain_totp_secret = secret
    return secret


def call(client, method, url, body=None, key=None, membership=None):
    """An API call as the browser makes it: JSON, X-Membership, and a request key on mutating methods."""
    headers = {}
    mid = membership if membership is not None else getattr(client, "membership_id", None)
    if mid is not None:
        headers["X-Membership"] = str(mid)
    if method != "GET":
        headers["Idempotency-Key"] = str(key or uuid.uuid4())
    payload = json.dumps(body or {}) if method != "GET" else None
    return client.generic(method, url, payload, content_type="application/json", headers=headers)
