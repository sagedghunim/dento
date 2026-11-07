from pathlib import Path

import polib
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Compile the .po translation files to .mo without needing GNU gettext."

    def handle(self, *args, **options):
        count = 0
        for locale_dir in settings.LOCALE_PATHS:
            for po_path in Path(locale_dir).rglob("*.po"):
                catalog = polib.pofile(str(po_path))
                catalog.save_as_mofile(str(po_path.with_suffix(".mo")))
                count += 1
                self.stdout.write(f"{po_path.relative_to(settings.BASE_DIR)}: {len(catalog.translated_entries())} strings")
        self.stdout.write(self.style.SUCCESS(f"Compiled {count} catalogue(s)."))
