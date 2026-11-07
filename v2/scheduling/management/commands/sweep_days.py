import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from scheduling.jobs import run_sweep


class Command(BaseCommand):
    help = ("Close the visits of business days that ended at least 30 minutes ago (design 5.10): "
            "booked and waiting become no-shows, visits still with the doctor are finished and flagged.")

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, action="append", help="Only this clinic id (repeatable).")
        parser.add_argument("--now", help="Pretend it is this ISO 8601 time (with its offset), for catch-up runs.")

    def handle(self, *args, **options):
        now = None
        if options["now"]:
            now = dt.datetime.fromisoformat(options["now"])
            if now.tzinfo is None:
                raise CommandError("--now needs an offset, e.g. 2026-11-08T05:00:00+02:00")
        totals = run_sweep(now=now, clinic_ids=set(options["clinic"] or []))
        self.stdout.write(self.style.SUCCESS(f"Swept: {totals or 'nothing to close'}"))
