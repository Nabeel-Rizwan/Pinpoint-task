"""Unit tests for the normalisation helpers.

Covers the three things the product's correctness hinges on: stable person
identity, partner-title classification, and corporate/M&A practice filtering.
"""

from __future__ import annotations

import unittest

from pinpoint import normalize as n


class IdentityTests(unittest.TestCase):
    def test_name_key_is_stable_across_formatting(self):
        # Order, punctuation, diacritics and suffixes must all collapse to one key.
        key = n.name_key("Jane Q. Doe")
        self.assertEqual(key, n.name_key("Doe, Jane Q."))
        self.assertEqual(key, n.name_key("  Jane   Q   Doe "))
        self.assertEqual(n.name_key("Jose Ramirez"), n.name_key("José Ramírez"))
        self.assertEqual(n.name_key("John Smith, Jr."), n.name_key("John Smith"))

    def test_person_key_is_namespaced_by_firm(self):
        self.assertEqual(n.person_key("firm-a", "Jane Doe"), "firm-a::jane doe")
        self.assertNotEqual(n.person_key("a", "Jane Doe"), n.person_key("b", "Jane Doe"))


class TitleTests(unittest.TestCase):
    def test_partner_titles_are_kept(self):
        for title in ["Partner", "Managing Partner", "Senior Partner", "Equity Partner"]:
            self.assertTrue(n.is_partner(title), title)

    def test_non_partner_titles_are_dropped(self):
        for title in ["Associate", "Counsel", "Of Counsel", "", "Head of Partnership"]:
            self.assertFalse(n.is_partner(title), title)

    def test_retired_and_former_partners_are_excluded(self):
        # The current-roster view must drop people who are no longer partners.
        for title in ["Retired Partner", "Partner Emeritus", "Former Partner"]:
            self.assertEqual(n.classify_title(title), "inactive")
            self.assertFalse(n.is_partner(title), title)


class PracticeTests(unittest.TestCase):
    def test_corporate_practice_canonicalisation_and_filtering(self):
        self.assertEqual(n.canonical_practice("Mergers & Acquisitions"), "M&A")
        self.assertEqual(n.canonical_practice("Securities"), "Capital Markets")
        self.assertIsNone(n.canonical_practice("Litigation"))

        self.assertTrue(n.matches_corporate_practice(["Corporate"]))
        self.assertTrue(n.matches_corporate_practice(["M&A"]))
        self.assertFalse(n.matches_corporate_practice(["Litigation", "Tax"]))
        self.assertFalse(n.matches_corporate_practice([]))

    def test_practices_inferred_from_text(self):
        found = n.practices_in_text("Advises on mergers and acquisitions and capital markets")
        self.assertIn("M&A", found)
        self.assertIn("Capital Markets", found)

    def test_canonical_office_handles_abbreviations(self):
        self.assertEqual(n.canonical_office("new york"), "New York")
        self.assertEqual(n.canonical_office("washington, d.c."), "Washington, D.C.")
        self.assertEqual(n.canonical_office("Washington DC"), "Washington, D.C.")
        self.assertEqual(n.canonical_office("Wilmington, DE"), "Wilmington")


if __name__ == "__main__":
    unittest.main()
