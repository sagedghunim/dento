from django.apps import AppConfig
from django.db.models.signals import post_migrate


def set_sequence_ranges(using, **kwargs):
    """Every identity sequence on a shard draws from its own range (design 3.3).

    Ids then never collide across shards, so a clinic can move with its ids.
    MAXVALUE makes exhaustion fail instead of colliding.
    """
    from django.conf import settings
    from django.db import connections

    from core.routing import id_range

    if using not in settings.SHARDS:
        return
    low, high = id_range(using)
    with connections[using].cursor() as cursor:
        cursor.execute(
            "SELECT sequencename, last_value, min_value, max_value FROM pg_sequences WHERE schemaname = current_schema()"
        )
        for name, last_value, min_value, max_value in cursor.fetchall():
            if min_value == low and max_value == high:
                continue
            restart = f" RESTART WITH {low}" if last_value is None or last_value < low else ""
            cursor.execute(f'ALTER SEQUENCE "{name}" MINVALUE {low} MAXVALUE {high} START WITH {low}{restart}')


class CoreConfig(AppConfig):
    name = "core"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        post_migrate.connect(set_sequence_ranges, dispatch_uid="core.set_sequence_ranges")
