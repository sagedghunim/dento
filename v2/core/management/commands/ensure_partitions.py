from django.core.management.base import BaseCommand

from core.partitions import MONTHS_AHEAD, ensure_all


class Command(BaseCommand):
    help = ("Create the missing monthly partitions of the log tables on the directory and every shard (design 4.10), "
            f"keeping the current month and {MONTHS_AHEAD} more. The worker runs it daily; migrate_all runs it too.")

    def handle(self, *args, **options):
        for alias, tables in ensure_all().items():
            self.stdout.write(self.style.SUCCESS(f"{alias}: future partitions {tables}"))
