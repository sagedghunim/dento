"""Fake clinical detail for a few patients in each demo place, after seed_activity (demo data only, never real).

Picks 20 patients per place (spread over the doctors, the ones with the most finished visits) and gives each of
their finished visits a doctor's note, about half of them a prescription from the section's library, and a few
of them a fake x-ray picture. Visits are closed, so the rows are written directly, as the doctor would have
written them that day. Running it again adds nothing to a visit that already has a note.

--no-files skips the pictures (a server whose disk does not keep files, such as the Vercel demo).
"""

import random
import struct
import zlib

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand
from django.utils import timezone

from clinical import services as clinical
from clinics.models import Clinic, Membership
from clinics.permissions import effective_codes
from core import sql
from core.context import Ctx
from core.routing import clinic_transaction
from directory.models import DirectoryClinic

from .seed_demo import DEMO

SEED = 20261117
PER_PLACE = 20

NOTES = {
    "dental": [
        ("ألم في الضرس اللي تحت شمال مع الحاجة الساقعة", "تسوس عميق في 36", "اتعمل حشو مؤقت. المريض يرجع بعد أسبوع للحشو النهائي."),
        ("نزيف في اللثة وقت التفريش", "التهاب لثة بسيط", "تنظيف جير وتلميع. مضمضة مرتين في اليوم لمدة أسبوع."),
        ("سنة مكسورة من قدام بعد خبطة", "كسر في حافة 11", "ترميم تجميلي بالكومبوزيت. تجنب الأكل الناشف على السنة دي."),
        ("ورم خفيف في الخد اليمين", "خراج حول جذر 46", "فتح الخراج وتنظيف. مضاد حيوي، ويبدأ علاج عصب الزيارة الجاية."),
        ("متابعة علاج العصب", "علاج عصب 46 الجلسة التانية", "تنظيف وتوسيع القنوات وحشو الكالسيوم. الجلسة الأخيرة بعد أسبوعين."),
        ("حساسية من الحلو والساقع", "انحسار لثة وتآكل عند العنق", "معجون للحساسية وفلورايد موضعي. تفريش بفرشة ناعمة."),
        ("عايز يركب طربوش", "تحضير 36 للتركيبة", "اتاخد المقاس واتركب طربوش مؤقت. التركيب النهائي بعد 10 أيام."),
        ("فحص دوري", "الأسنان سليمة", "تنظيف دوري. الزيارة الجاية بعد 6 شهور."),
    ],
    "pediatrics": [
        ("سخونية وكحة من يومين", "التهاب في الحلق", "خافض حرارة وسوائل كتير. يرجع لو السخونية ما نزلتش بعد 3 أيام."),
        ("إسهال وترجيع", "نزلة معوية", "محلول جفاف ونظام أكل خفيف. مراقبة علامات الجفاف."),
        ("متابعة وزن وطول", "نمو طبيعي", "الوزن والطول في المعدل. التطعيم الجاي في ميعاده."),
        ("رشح وانسداد في المناخير", "نزلة برد", "محلول ملح للمناخير وراحة. مفيش داعي لمضاد حيوي."),
        ("طفح جلدي في الوش", "حساسية جلدية", "كريم مرطب وتجنب الصابون المعطر. يرجع لو الطفح زاد."),
        ("ألم في الودن", "التهاب الأذن الوسطى", "مضاد حيوي 7 أيام ومسكن. متابعة بعد أسبوع."),
    ],
    "dermatology": [
        ("حبوب في الوش", "حب شباب متوسط", "غسول وكريم موضعي. ممنوع الضغط على الحبوب. متابعة بعد شهر."),
        ("بقع غامقة في الوش", "كلف", "واقي شمس يومي وكريم تفتيح بالليل. النتيجة بتبان بعد 6 أسابيع."),
        ("هرش في الإيد", "إكزيما", "كريم كورتيزون خفيف أسبوع ومرطب على طول."),
        ("تساقط شعر", "تساقط موسمي", "تحليل فيتامين د وحديد. مكمل غذائي ولوشن للشعر."),
        ("متابعة", "تحسن ملحوظ", "استمرار على نفس العلاج شهر كمان."),
    ],
}
GENERAL = [
    ("كشف عام", "حالة مستقرة", "متابعة بعد شهر."),
    ("صداع متكرر", "صداع توتري", "مسكن عند اللزوم وراحة كفاية. يقلل الكافيين."),
]
RX_NOTES = ("بعد الأكل", "لمدة 5 أيام", "عند اللزوم", "")


def png(width, height, seed):
    """A grey picture that looks a little like a dental x-ray: dark around, light shapes in the middle."""
    rng = random.Random(seed)
    teeth = [(rng.randint(30, width - 30), rng.randint(height // 3, 2 * height // 3), rng.randint(14, 22))
             for _ in range(5)]
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            value = 25 + int(25 * (1 - abs(y - height / 2) / (height / 2)))
            for cx, cy, r in teeth:
                d = ((x - cx) / r) ** 2 + ((y - cy) / (r * 2.2)) ** 2
                if d < 1:
                    value = max(value, 210 - int(80 * d))
            rows.append(min(255, value + rng.randint(0, 12)))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


class Command(BaseCommand):
    help = "Fake notes, prescriptions and x-rays for 20 patients per demo place (demo data only)."

    def add_arguments(self, parser):
        parser.add_argument("--no-files", action="store_true", help="Skip the x-ray pictures.")

    def handle(self, *args, **options):
        names = {spec["name"] for spec in DEMO}
        for place in DirectoryClinic.objects.filter(name__in=names).order_by("id"):
            with clinic_transaction(place.id, place.shard):
                self.stdout.write(f"{place.name}: {self.fill(place, not options['no_files'])}")

    def fill(self, place, files):
        clinic = Clinic.objects.using(place.shard).get(id=place.id)
        members = {m.id: m for m in Membership.objects.using(place.shard).filter(clinic_id=clinic.id)}
        rng = random.Random(SEED + clinic.id)
        visits = sql.dicts(Ctx(clinic=clinic, membership=None, user_id=None, actor="system", alias=place.shard), """
            SELECT a.id, a.patient_id, a.doctor_id, a.section_id, a.day, a.finished_at AS ended_at, s.specialty_code
              FROM "scheduling_appointment" a
              JOIN "clinics_section" s ON s.clinic_id = a.clinic_id AND s.id = a.section_id
             WHERE a.clinic_id = %s AND a.status = 'done' ORDER BY a.day, a.id""", [clinic.id])
        by_patient = {}
        for v in visits:
            by_patient.setdefault(v["patient_id"], []).append(v)
        # Round-robin over the doctors, the patients with the most finished visits first.
        per_doctor = {}
        for patient_id, rows in sorted(by_patient.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            per_doctor.setdefault(rows[-1]["doctor_id"], []).append(patient_id)
        chosen, queues = [], [list(q) for q in per_doctor.values()]
        while len(chosen) < PER_PLACE and any(queues):
            for queue in queues:
                if queue and len(chosen) < PER_PLACE:
                    chosen.append(queue.pop(0))
        notes = prescriptions = pictures = 0
        for patient_id in chosen:
            for index, v in enumerate(by_patient[patient_id]):
                member = members.get(v["doctor_id"])
                if member is None:
                    continue
                ctx = Ctx(clinic=clinic, membership=member, user_id=member.user_id, actor="member",
                          codes=effective_codes(member.role, member.granted, member.revoked), mfa_verified=True,
                          request_id="seed_files", alias=place.shard)
                at = v["ended_at"] or timezone.now()
                complaint, diagnosis, text = rng.choice(NOTES.get(v["specialty_code"], GENERAL))
                added = sql.row(ctx, """
                    INSERT INTO "clinical_visit_note" (clinic_id, appointment_id, patient_id, complaint, diagnosis, notes,
                                                       version, created_at, updated_at, updated_by_id)
                    VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s)
                    ON CONFLICT (clinic_id, appointment_id) DO NOTHING RETURNING id""", [
                    clinic.id, v["id"], patient_id, complaint, diagnosis, text, at, at, member.id])
                if added is None:
                    continue
                notes += 1
                if rng.random() < 0.5:
                    prescriptions += self.prescription(ctx, rng, v, at)
                if files and v["specialty_code"] == "dental" and (index == 0 or rng.random() < 0.2):
                    title = "أشعة بانوراما" if index == 0 else "أشعة صغيرة"
                    upload = SimpleUploadedFile(f"xray-{v['id']}.png", png(220, 140, v["id"]), "image/png")
                    attachment = clinical.add_attachment(ctx, patient_id, v["id"], title, upload)
                    sql.run(ctx, 'UPDATE "clinical_attachment" SET created_at = %s WHERE clinic_id = %s AND id = %s',
                            [at, clinic.id, attachment])
                    pictures += 1
        return f"{len(chosen)} patients, {notes} notes, {prescriptions} prescriptions, {pictures} x-rays"

    def prescription(self, ctx, rng, v, at):
        medicines = sql.dicts(ctx, 'SELECT id, name, generic, strength, form, default_dose FROM "clinical_medicine" '
                                   "WHERE clinic_id = %s AND section_id = %s AND is_active ORDER BY id LIMIT 40",
                              [ctx.clinic_id, v["section_id"]])
        if not medicines:
            return 0
        number = clinical.take_numbers(ctx, "prescription")[0]
        prescription_id = sql.row(ctx, """
            INSERT INTO "clinical_prescription" (clinic_id, number, patient_id, appointment_id, doctor_id, section_id,
                                                 notes, created_at, created_by_id)
            VALUES (%s, %s, %s, %s, %s, %s, '', %s, %s) RETURNING id""", [
            ctx.clinic_id, number, v["patient_id"], v["id"], v["doctor_id"], v["section_id"], at, ctx.membership_id])[0]
        for position, m in enumerate(rng.sample(medicines, min(len(medicines), rng.randint(1, 3))), start=1):
            sql.run(ctx, """
                INSERT INTO "clinical_prescription_item" (clinic_id, prescription_id, position, medicine_id, name, generic,
                                                          strength, form, dose, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""", [
                ctx.clinic_id, prescription_id, position, m["id"], m["name"], m["generic"], m["strength"], m["form"],
                m["default_dose"], rng.choice(RX_NOTES)])
        return 1
