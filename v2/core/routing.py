"""Which database a query goes to (design 3.3).

- Directory apps live only in `default`.
- Tenant apps live in every shard. Their queries go to the shard of the clinic
  in the current context, which `clinic_transaction` sets. A tenant query
  without a context raises: there is never a default shard.
"""

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass

from django.conf import settings
from django.db import connections, transaction

DIRECTORY_APPS = frozenset({"accounts", "directory", "procrastinate", "contenttypes", "auth", "sessions"})

# Every identity sequence on shard s draws from [s * 2^48, (s + 1) * 2^48).
SHARD_ID_BITS = 48


class NoClinicContext(RuntimeError):
    """A tenant query ran outside clinic_transaction()."""


@dataclass(frozen=True)
class ClinicScope:
    clinic_id: int
    alias: str


_scope = contextvars.ContextVar("hamada_clinic_scope", default=None)


def current_scope():
    scope = _scope.get()
    if scope is None:
        raise NoClinicContext("A tenant query needs a clinic context (core.routing.clinic_transaction).")
    return scope


def is_directory_app(app_label):
    return app_label in DIRECTORY_APPS


def shard_aliases():
    return list(settings.SHARDS)


def shard_number(alias):
    return settings.SHARDS[alias]


def id_range(alias):
    n = shard_number(alias)
    return n << SHARD_ID_BITS, ((n + 1) << SHARD_ID_BITS) - 1


class ShardRouter:
    def _alias(self, model):
        if is_directory_app(model._meta.app_label):
            return "default"
        return current_scope().alias

    def db_for_read(self, model, **hints):
        return self._alias(model)

    def db_for_write(self, model, **hints):
        return self._alias(model)

    def allow_relation(self, obj1, obj2, **hints):
        # Rows only relate inside one database.
        return obj1._state.db == obj2._state.db

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if is_directory_app(app_label):
            return db == "default"
        return db in settings.SHARDS


def set_clinic_setting(alias, clinic_id):
    """Row-level security reads app.clinic_id; it lasts until the transaction ends (design 4.0)."""
    with connections[alias].cursor() as cursor:
        cursor.execute("SELECT set_config('app.clinic_id', %s, true)", [str(clinic_id) if clinic_id else ""])


@contextmanager
def clinic_scope(clinic_id, alias):
    """Route tenant queries to `alias` without opening a transaction (used inside one)."""
    token = _scope.set(ClinicScope(clinic_id, alias))
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def clinic_transaction(clinic_id, alias):
    """One transaction on the clinic's shard, with the RLS setting made first."""
    with clinic_scope(clinic_id, alias), transaction.atomic(using=alias):
        set_clinic_setting(alias, clinic_id)
        yield
