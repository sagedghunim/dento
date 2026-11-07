from django.core.management.base import BaseCommand

from scheduling.jobs import run_recompute


class Command(BaseCommand):
    help = ("Recompute doctor-day and clinic-day summaries of past days changed since the last run (design 5.10), "
            "one clinic-day per transaction. Differences are fixed and logged.")

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, action="append", help="Only this clinic id (repeatable).")

    def handle(self, *args, **options):
        found = run_recompute(clinic_ids=set(options["clinic"] or []))
        style = self.style.WARNING if found else self.style.SUCCESS
        self.stdout.write(style(f"Differences fixed: {len(found)}"))
        for difference in found:
            self.stdout.write(f"  {difference}")
