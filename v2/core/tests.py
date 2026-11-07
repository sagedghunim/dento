from django.test import SimpleTestCase

from core.text import ascii_digits, is_valid_phone, looks_like_phone, normalize_name, normalize_phone


class NormalizeNameTests(SimpleTestCase):
    def test_alef_forms_taa_marbuta_and_alef_maqsura_match(self):
        self.assertEqual(normalize_name("أحمد"), normalize_name("احمد"))
        self.assertEqual(normalize_name("إيمان"), normalize_name("ايمان"))
        self.assertEqual(normalize_name("آمال"), normalize_name("امال"))
        self.assertEqual(normalize_name("فاطمة"), normalize_name("فاطمه"))
        self.assertEqual(normalize_name("مصطفى"), normalize_name("مصطفي"))

    def test_diacritics_tatweel_and_spaces_are_removed(self):
        self.assertEqual(normalize_name("  مُحَمَّد   عــلي "), "محمد علي")

    def test_latin_names_fold_case(self):
        self.assertEqual(normalize_name("  Omar   KHALIL "), "omar khalil")

    def test_arabic_digits(self):
        self.assertEqual(ascii_digits("٠١٢٣٤٥٦٧٨٩"), "0123456789")


class PhoneTests(SimpleTestCase):
    def test_egyptian_forms_become_national(self):
        for raw in ["01012345678", "+201012345678", "00201012345678", "201012345678", "1012345678", "٠١٠١٢٣٤٥٦٧٨", "010 1234 5678"]:
            self.assertEqual(normalize_phone(raw), "01012345678", raw)

    def test_validity(self):
        self.assertTrue(is_valid_phone("01012345678"))
        self.assertFalse(is_valid_phone("01312345678"))
        self.assertFalse(is_valid_phone("0101234567"))
        self.assertTrue(is_valid_phone("966501234567"))
        self.assertFalse(is_valid_phone(""))

    def test_looks_like_phone(self):
        self.assertTrue(looks_like_phone("010 123"))
        self.assertTrue(looks_like_phone("٠١٠"))
        self.assertFalse(looks_like_phone("محمد"))
        self.assertFalse(looks_like_phone("  "))
