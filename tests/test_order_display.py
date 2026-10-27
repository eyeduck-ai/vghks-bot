"""Known ophthalmic names affect display priority, never clinical selection."""
import copy
import unittest

from vghks_bot.order_display import COMMON_OPHTHALMIC_ORDERS, display_group
from vghks_bot.review_history import ReviewHistory


class OrderDisplayTests(unittest.TestCase):
    def test_known_names_are_case_insensitive_without_matching_embedded_substrings(self):
        for name in COMMON_OPHTHALMIC_ORDERS:
            with self.subTest(name=name):
                self.assertTrue(display_group(" " + name.swapcase() + " ")["ophthalmic"])
        self.assertFalse(display_group("unrelated FAGRAPHY report")["ophthalmic"])
        self.assertFalse(display_group("未知檢查")["ophthalmic"])

    def test_approved_aliases_use_word_boundaries_and_do_not_guess_from_department(self):
        for name in ("OCT", "Optical coherence tomography", "光學同調斷層檢查", "Visual Field",
                     "Perimetry", "視野檢查", "Fundus photo", "Fundus photography", "Fundus image",
                     "Fundus Color Photo Picture, eac", "眼底攝影"):
            with self.subTest(name=name):
                self.assertTrue(display_group(name)["ophthalmic"])
        for name in ("Octreotide", "preOCTsample", "Visual Fielding", "PerimetryOther", "Fundus",
                     "gastric fundus", "gastric fundus image", "胃 Fundus photo", "FundusOther photo",
                     "OR", "DEPARTMENTAL", "眼科其他未辨識檢查"):
            with self.subTest(name=name):
                self.assertFalse(display_group(name)["ophthalmic"])

    def test_common_groups_precede_others_and_keep_dates_all_rows_and_raw_sources(self):
        groups = {key: [{"name": name, "order_date": day, "columns": {"院方欄位": "保留"}}]
                  for key, name, day in (("other-new", "其他檢查", "2026-10-27"),
                      ("eye-old", "FAG", "2024-01-01"),
                      ("eye-new", "DBR, free charge", "2026-10-20"),
                      ("other-old", "其他檢查", "2020-01-01"))}
        original = copy.deepcopy(groups)
        rows = ReviewHistory._public_orders(groups)
        self.assertEqual([row["id"] for row in rows], ["eye-new", "eye-old", "other-new", "other-old"])
        self.assertEqual([row["display_group"] for row in rows], ["常用眼科檢查"] * 2 + ["其他醫囑"] * 2)
        self.assertEqual(groups, original)
