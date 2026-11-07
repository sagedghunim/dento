"""Finding the translatable strings without GNU gettext (design 7.2).

`makemessages` needs xgettext, which isn't available on Windows. This extracts the
same strings by pattern; a test fails when Arabic lacks one or keeps an unused one.
"""

import re
from pathlib import Path

from django.conf import settings

_TEMPLATE = re.compile(r'{%\s*translate\s+"((?:[^"\\]|\\.)*)"')
_PYTHON = re.compile(r'(?<![\w.])(?:_|gettext|gettext_lazy)\(\s*"((?:[^"\\]|\\.)*)"')
_SKIP_PARTS = {".venv", "migrations", "staticfiles", "locale"}
_SKIP_FILES = {"tests.py", "testing.py", "i18n.py"}


def extract_msgids(base=None):
    base = Path(base or settings.BASE_DIR)
    found = []
    for path in sorted(base.rglob("*")):
        if _SKIP_PARTS & set(path.parts) or path.name in _SKIP_FILES or path.name.startswith("test_"):
            continue
        if path.suffix == ".html":
            pattern = _TEMPLATE
        elif path.suffix == ".py":
            pattern = _PYTHON
        else:
            continue
        for match in pattern.findall(path.read_text(encoding="utf-8")):
            text = match.replace('\\"', '"')
            if text not in found:
                found.append(text)
    return found


def catalogue_path(language="ar"):
    return Path(settings.BASE_DIR) / "locale" / language / "LC_MESSAGES" / "django.po"
