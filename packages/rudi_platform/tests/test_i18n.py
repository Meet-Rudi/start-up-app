"""
The string catalogue — platform texts, clone overrides, fallbacks, publish-time coverage.

What this guards: a white-label clone greeting as "Rudi", a typo'd placeholder reaching a person
mid-chat, and a clone that serves Polish but greets in English without anyone noticing.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.guards.leakguard import SAFE_REPLY
from rudi_platform.i18n import (
    PLATFORM_STRINGS,
    SPECS,
    Catalogue,
    I18nError,
    normalize_locale,
    placeholders_in,
)


class PlatformStrings(unittest.TestCase):
    def test_every_locale_has_every_key_with_allowed_placeholders(self):
        for locale, table in PLATFORM_STRINGS.items():
            self.assertEqual(set(table), set(SPECS), locale)
            for key, text in table.items():
                self.assertLessEqual(
                    placeholders_in(text), SPECS[key].placeholders, (locale, key)
                )

    def test_the_persona_is_whoever_the_clone_says(self):
        cat = Catalogue()
        self.assertIn("I am Emma.", cat.text("intro", "en", persona="Emma"))
        self.assertIn("Ik ben Emma.", cat.text("intro", "nl-BE", persona="Emma"))
        self.assertNotIn("Rudi", "".join(PLATFORM_STRINGS["en"].values()))

    def test_leak_redirect_matches_the_guard(self):
        cat = Catalogue()
        for locale, text in SAFE_REPLY.items():
            self.assertEqual(cat.text("leak_redirect", locale, persona="x"), text)


class Lookup(unittest.TestCase):
    def test_fallback_chain(self):
        cat = Catalogue({"nudge": {"nl": "Hoi daar!"}}, default_locale="nl")
        self.assertEqual(cat.text("nudge", "nl", persona="x"), "Hoi daar!")
        self.assertEqual(
            cat.text("nudge", "pl", persona="x"), "Hoi daar!"
        )  # clone default
        self.assertTrue(
            cat.text("nudge", "en", persona="x").startswith("👋 Just checking in")
        )
        self.assertTrue(
            Catalogue()
            .text("nudge", None, persona="x")
            .startswith("👋 Just checking in")
        )

    def test_values(self):
        cat = Catalogue()
        self.assertIn("photo", cat.text("media_ack", "en", persona="x", kind="photo"))
        self.assertEqual(
            cat.text("generic_goal", "fr", persona="ignored"), "notre objectif"
        )
        with self.assertRaises(I18nError):
            cat.text("intro", "en")  # no persona
        with self.assertRaises(I18nError):
            cat.text("no_such_key", "en")


class Overrides(unittest.TestCase):
    def test_bad_overrides_fail_when_the_version_is_built(self):
        for overrides in (
            {"greeting": {"en": "hi"}},  # unknown key
            {"intro": {"en": "Hi {name}!"}},  # placeholder not allowed for intro
            {"intro": {"en": "Hi {}!"}},  # positional
            {"intro": {"en": "Hi {persona"}},  # malformed
            {"intro": {"en": "  "}},  # empty
            {"intro": {"english": "Hi"}},  # bad locale
            {"intro": "Hi"},  # not per-locale
        ):
            with self.subTest(overrides=overrides), self.assertRaises(I18nError):
                Catalogue(overrides)

    def test_missing_coverage(self):
        self.assertEqual(Catalogue().missing(["en", "nl-BE"]), {})
        gaps = Catalogue({"intro": {"pl": "Cześć, jestem {persona}!"}}).missing(["pl"])
        self.assertEqual(set(gaps["pl"]), set(SPECS) - {"intro"})

    def test_normalize_locale(self):
        self.assertEqual(normalize_locale("nl_BE"), "nl")
        self.assertEqual(normalize_locale(" FR-fr "), "fr")
        for bad in (None, "", "x", "dutch", "12", 5):
            self.assertIsNone(normalize_locale(bad), bad)


if __name__ == "__main__":
    unittest.main()
