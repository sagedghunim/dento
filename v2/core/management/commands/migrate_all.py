from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connections
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = "Migrate the directory and every shard (design 3.3), then report each one's state."

    def handle(self, *args, **options):
        aliases = ["default", *settings.SHARDS]
        for alias in aliases:
            self.stdout.write(self.style.MIGRATE_HEADING(f"== {alias} ({settings.DATABASES[alias]['NAME']})"))
            call_command("migrate", database=alias, interactive=False, verbosity=max(options["verbosity"] - 1, 0))
        failed = False
        for alias in aliases:
            executor = MigrationExecutor(connections[alias])
            pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
            if pending:
                failed = True
                self.stdout.write(self.style.ERROR(f"{alias}: {len(pending)} migrations not applied"))
            else:
                self.stdout.write(self.style.SUCCESS(f"{alias}: up to date"))
        if failed:
            raise SystemExit(1)
