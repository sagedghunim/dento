"""Specialty templates (design 4.2, E2, E4): copied into a section when it is created or changes specialty.

Data, not tables: they never vary per clinic until copied, and a clinic edits its own copy. The data lives in
`clinics/catalogue/` (dental.py, others.py). Here each specialty has:
- `name` (ar, en);
- `services`: what the desk books (Arabic, English, kind, price in piastres, minutes);
- `procedures`: what the doctor records (dicts, prices in piastres, with a description where one is written);
- `medicines`: the medicine library (dicts);
- `templates`: named prescriptions (name, lines as (trade name, strength), notes).
`CARDS` is the order of the sign-up cards (design E2). `internal` stays for places created with it.
"""

from .catalogue import dental, others
from .catalogue.english import DENTAL_DESCRIPTIONS, english

CARDS = ["dental", "pediatrics", "dermatology", "obgyn", "ent", "ophthalmology", "orthopedics", "cardiology",
         "physiotherapy", "veterinary"]


def _build(name, services, procedures, medicines, templates=(), descriptions=None, descriptions_en=None):
    descriptions, descriptions_en = descriptions or {}, descriptions_en or {}
    return {
        "name": name,
        "services": [(ar, en, kind, price * 100, minutes) for ar, en, kind, price, minutes in services],
        "procedures": [{"name_ar": ar, "name_en": en, "category": category, "unit": unit, "price": price * 100,
                        "typical_min": low * 100, "typical_max": high * 100, "condition": condition,
                        "description": descriptions.get(en, ""), "description_en": descriptions_en.get(en, "")}
                       for ar, en, category, unit, price, low, high, condition in procedures],
        "medicines": [{"name": name_, "generic": generic, "strength": strength, "form": form, "default_dose": dose,
                       "default_note": note, "form_en": english(form), "default_dose_en": english(dose),
                       "default_note_en": english(note)}
                      for name_, generic, strength, form, dose, note in medicines],
        "templates": list(templates),
    }


SPECIALTIES = {"dental": _build(dental.NAME, dental.SERVICES, dental.PROCEDURES, dental.MEDICINES, dental.TEMPLATES,
                               dental.DESCRIPTIONS, DENTAL_DESCRIPTIONS)}
for _code in [*CARDS[1:], "internal"]:
    _spec = others.SPECIALTIES[_code]
    SPECIALTIES[_code] = _build(_spec["name"], _spec["services"], _spec["procedures"], _spec["medicines"])


def choices():
    return list(SPECIALTIES)
