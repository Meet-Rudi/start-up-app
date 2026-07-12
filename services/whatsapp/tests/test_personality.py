"""
Personality store tests — OCEAN validation, S3 load, and deterministic rendering.

boto3 is stubbed to an in-memory FakeS3 (no network, no real PII — CLAUDE.md §8).

Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import os
import sys
import json
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from fake_s3 import FakeS3  # noqa: E402

_FAKE_S3 = FakeS3()
boto3_stub = types.ModuleType("boto3")
boto3_stub.client = lambda name, *a, **k: _FAKE_S3
sys.modules["boto3"] = boto3_stub
os.environ["DATA_BUCKET"] = "meetrudi-ai-data-test"

import personality  # noqa: E402

# personality may have been imported earlier (via console_api/processor) bound to a different
# FakeS3; rebind to our seeded instance for this file's tests.
personality._s3 = _FAKE_S3

BUCKET = "meetrudi-ai-data-test"

SEED_V2 = {"name": "Seed Rudi v2", "slug": "seed-rudi-v2", "version": 1,
           "ocean": {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19}, "briefing": None}


def _seed(slug, doc, briefing_name=None, briefing_body=None):
    _FAKE_S3.put_object(Bucket=BUCKET, Key="personalities/%s/personality.json" % slug,
                        Body=json.dumps(doc).encode("utf-8"))
    if briefing_name:
        _FAKE_S3.put_object(Bucket=BUCKET, Key="personalities/%s/%s" % (slug, briefing_name),
                            Body=(briefing_body or "").encode("utf-8"))


class TestValidation(unittest.TestCase):
    def setUp(self):
        personality._s3 = _FAKE_S3
        personality._cache.clear()

    def test_loads_seed_rudi_v2(self):
        _seed("seed-rudi-v2", SEED_V2)
        p = personality.load("seed-rudi-v2")
        self.assertEqual(p["ocean"], {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19})
        self.assertEqual(p["name"], "Seed Rudi v2")
        self.assertEqual(p["briefing_text"], "")

    def test_missing_dimension_raises(self):
        _seed("bad", {"ocean": {"O": 50, "C": 50, "E": 50, "A": 50}})
        with self.assertRaises(personality.PersonalityError):
            personality.load("bad")

    def test_out_of_range_raises(self):
        _seed("hi", {"ocean": {"O": 50, "C": 50, "E": 50, "A": 50, "N": 120}})
        with self.assertRaises(personality.PersonalityError):
            personality.load("hi")

    def test_missing_personality_raises(self):
        with self.assertRaises(personality.PersonalityError):
            personality.load("does-not-exist")

    def test_slug_traversal_rejected(self):
        with self.assertRaises(personality.PersonalityError):
            personality.load("../secrets")

    def test_optional_briefing_loaded(self):
        _seed("withbrief", {"ocean": {"O": 50, "C": 50, "E": 50, "A": 50, "N": 50},
                            "briefing": "briefing.md"}, "briefing.md", "Extra nuance here.")
        p = personality.load("withbrief")
        self.assertEqual(p["briefing_text"], "Extra nuance here.")

    def test_declared_briefing_missing_raises(self):
        _seed("brokenbrief", {"ocean": {"O": 50, "C": 50, "E": 50, "A": 50, "N": 50},
                              "briefing": "nope.md"})
        with self.assertRaises(personality.PersonalityError):
            personality.load("brokenbrief")


class TestRendering(unittest.TestCase):
    def setUp(self):
        personality._s3 = _FAKE_S3
        personality._cache.clear()
        _seed("seed-rudi-v2", SEED_V2)

    def test_band_boundaries(self):
        self.assertEqual(personality._band(20), "very low")
        self.assertEqual(personality._band(21), "low")
        self.assertEqual(personality._band(60), "moderate")
        self.assertEqual(personality._band(61), "high")
        self.assertEqual(personality._band(81), "very high")

    def test_block_is_deterministic(self):
        b1 = personality.load_block("seed-rudi-v2")
        personality._cache.clear()
        b2 = personality.load_block("seed-rudi-v2")
        self.assertEqual(b1, b2)

    def test_block_contains_scores_and_bands(self):
        block = personality.load_block("seed-rudi-v2")
        self.assertIn("Conscientiousness 71/100 (high)", block)
        self.assertIn("Emotional stability 81/100 — Neuroticism 19/100 (very high)", block)
        self.assertIn("Openness 59/100 (moderate)", block)
        # Never leak the mechanism to the model's output.
        self.assertIn("Do NOT mention OCEAN", block)

    def test_block_omits_briefing_when_absent(self):
        block = personality.load_block("seed-rudi-v2")
        self.assertNotIn("Persona briefing", block)


if __name__ == "__main__":
    unittest.main()
