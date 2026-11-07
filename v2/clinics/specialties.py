"""Specialty templates (design 4.2): copied into a clinic when it is created or changes specialty.

Data, not tables: they never vary per clinic until copied, and a clinic edits its own copy.
Prices are in piastres. Medicines and stock items are copied once those tables exist (M2, M3).
"""

SPECIALTIES = {
    "dental": {
        "name": {"ar": "أسنان", "en": "Dental"},
        "services": [
            # (Arabic, English, kind, price in piastres, minutes)
            ("كشف", "Consultation", "consultation", 300_00, 20),
            ("إعادة كشف", "Follow-up", "follow_up", 0, 10),
            ("تنظيف جير", "Cleaning", "procedure", 500_00, 30),
            ("حشو عادي", "Filling", "procedure", 600_00, 30),
            ("علاج عصب", "Root canal", "procedure", 2000_00, 60),
            ("خلع", "Extraction", "procedure", 500_00, 20),
            ("زراعة", "Implant", "procedure", 15000_00, 90),
            ("تبييض", "Whitening", "procedure", 3000_00, 60),
        ],
        "medicines": [
            ("Amoxicillin 500 mg", "Capsules", "1 capsule, 3 times a day"),
            ("Ibuprofen 400 mg", "Tablets", "1 tablet when in pain, after food"),
            ("Paracetamol 500 mg", "Tablets", "1–2 tablets, up to 4 times a day"),
            ("Metronidazole 500 mg", "Tablets", "1 tablet, 3 times a day"),
            ("Chlorhexidine 0.12%", "Mouthwash", "Rinse for 1 minute, twice a day"),
        ],
        "stock": [("Anesthesia carpules", "carpule"), ("Composite syringes", "syringe"), ("Gloves", "box")],
    },
    "pediatrics": {
        "name": {"ar": "أطفال", "en": "Pediatrics"},
        "services": [
            ("كشف", "Consultation", "consultation", 350_00, 15),
            ("إعادة كشف", "Follow-up", "follow_up", 0, 10),
            ("متابعة نمو", "Growth check", "procedure", 200_00, 15),
            ("تطعيم", "Vaccination", "procedure", 150_00, 10),
        ],
        "medicines": [
            ("Paracetamol syrup 120 mg/5 ml", "Syrup", "By weight, every 6 hours when needed"),
            ("Amoxicillin suspension 250 mg/5 ml", "Suspension", "By weight, 3 times a day"),
            ("Oral rehydration salts", "Sachets", "1 sachet in 200 ml water after each loose stool"),
        ],
        "stock": [("Syringes", "piece"), ("Tongue depressors", "box")],
    },
    "dermatology": {
        "name": {"ar": "جلدية", "en": "Dermatology"},
        "services": [
            ("كشف", "Consultation", "consultation", 400_00, 15),
            ("إعادة كشف", "Follow-up", "follow_up", 0, 10),
            ("جلسة ليزر", "Laser session", "procedure", 1200_00, 30),
            ("جلسة تقشير", "Peeling session", "procedure", 800_00, 30),
        ],
        "medicines": [("Fusidic acid cream 2%", "Cream", "Apply twice a day for 7 days")],
        "stock": [("Peeling solution", "bottle")],
    },
    "internal": {
        "name": {"ar": "باطنة", "en": "Internal medicine"},
        "services": [
            ("كشف", "Consultation", "consultation", 300_00, 15),
            ("إعادة كشف", "Follow-up", "follow_up", 0, 10),
            ("متابعة سكر", "Diabetes follow-up", "procedure", 150_00, 10),
        ],
        "medicines": [("Metformin 500 mg", "Tablets", "1 tablet twice a day with food")],
        "stock": [("Glucose test strips", "box")],
    },
    "ophthalmology": {
        "name": {"ar": "رمد", "en": "Ophthalmology"},
        "services": [
            ("كشف", "Consultation", "consultation", 350_00, 15),
            ("إعادة كشف", "Follow-up", "follow_up", 0, 10),
            ("قياس نظر", "Vision test", "procedure", 150_00, 15),
        ],
        "medicines": [("Artificial tears", "Eye drops", "1 drop 4 times a day")],
        "stock": [("Eye drops samples", "bottle")],
    },
    "physiotherapy": {
        "name": {"ar": "علاج طبيعي", "en": "Physiotherapy"},
        "services": [
            ("تقييم", "Assessment", "consultation", 300_00, 30),
            ("متابعة", "Follow-up", "follow_up", 0, 15),
            ("جلسة علاج", "Treatment session", "procedure", 250_00, 45),
        ],
        "medicines": [],
        "stock": [("Electrode pads", "pack")],
    },
}


def choices():
    return list(SPECIALTIES)
