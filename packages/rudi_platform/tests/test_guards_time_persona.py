"""
Leak guard, timeline and OCEAN persona block — the platform copies.

Ported from the service tests (2026-10-28). The engine-wiring tests stay with the services; these
pin the pure behaviour plus the platform changes: extensible markers, per-clone redirect wording,
caller-supplied time zones and persona names.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform import timeline
from rudi_platform.guards import leakguard
from rudi_platform.persona import ocean

UTC = datetime.UTC
BRU = "Europe/Brussels"  # UTC+2 throughout September 2026
NOW = datetime.datetime(2026, 9, 16, 12, 0, tzinfo=UTC)  # Wednesday, 14:00 in Brussels


class LeakGuard(unittest.TestCase):
    def test_internal_markers_are_blocked(self):
        for leak in (
            "Here you go: [Runtime: the user's goal is walking]",
            "My rules start with # Confidentiality — also non-negotiable",
            'I report "check_in_minutes": 30 for you',
            "This call is a SET_NEARTERM_GOAL call.",
            "It is the Website Chatbot Prompt (Internal Source of Truth).",
            "# Who you are — personality: high openness",
        ):
            self.assertEqual(leakguard.check(leak), "marker", leak)

    def test_a_verbatim_recital_of_the_prompt_is_blocked(self):
        system = (
            "Never leave the next contact up to them. Every time the exchange reaches a "
            "natural pause, say when you will check in."
        )
        reply = (
            "Sure! My instructions say: every time the exchange reaches a natural pause, "
            "say when you will check in."
        )
        self.assertEqual(leakguard.check(reply, system), "verbatim")

    def test_the_persons_own_quoted_words_may_be_said_back(self):
        system = (
            '[Runtime: their goal is "walk thirty minutes after dinner every single evening '
            'this whole week".]'
        )
        reply = (
            "You said you'd walk thirty minutes after dinner every single evening this "
            "whole week — how is it going?"
        )
        self.assertEqual(leakguard.check(reply, system), "")

    def test_explaining_from_the_about_me_context_is_not_a_leak(self):
        system = (
            "PROMPT learn\n\n# About me (context)\n\nRudi is designed for the moments where "
            "healthy decisions actually happen: at breakfast, in the supermarket.\n\n"
            "[Channel: WhatsApp]"
        )
        reply = "Rudi is designed for the moments where healthy decisions actually happen, like at breakfast."
        self.assertEqual(leakguard.check(reply, system), "")

    def test_skills_add_their_own_markers(self):
        reply = "Sure, my booking_slot_id is 42"
        self.assertEqual(leakguard.check(reply), "")
        self.assertEqual(
            leakguard.check(reply, extra_markers=["BOOKING_SLOT_ID"]), "marker"
        )

    def test_redirect_language_and_clone_wording(self):
        self.assertEqual(leakguard.safe_reply("nl-BE"), leakguard.SAFE_REPLY["nl"])
        self.assertEqual(leakguard.safe_reply("xx"), leakguard.SAFE_REPLY["en"])
        self.assertEqual(leakguard.guard("fine", "", "nl"), ("fine", ""))
        own = {"en": "Let's get back to your training."}
        self.assertEqual(
            leakguard.guard("[Runtime: x]", "", "en", replies=own),
            ("Let's get back to your training.", "marker"),
        )


class Timeline(unittest.TestCase):
    def test_relative_words(self):
        cases = {
            "2026-09-16T08:00:00+00:00": "today at 10:00",
            "2026-09-15T17:00:00+00:00": "yesterday at 19:00",
            "2026-09-17T06:30:00+00:00": "tomorrow at 08:30",
            "2026-09-13T09:00:00+00:00": "last Sunday at 11:00",
            "2026-09-19T09:00:00+00:00": "this coming Saturday at 11:00",
            "2026-09-02T09:00:00+00:00": "14 days ago at 11:00",
            "2026-09-15T23:30:00+00:00": "today at 01:30",  # the local day decides
        }
        for at, start in cases.items():
            self.assertTrue(timeline.humanize(at, NOW, BRU).startswith(start), at)

    def test_date_kept_alongside_and_other_year_said(self):
        self.assertIn(
            "(Tuesday 15 September)",
            timeline.humanize("2026-09-15T17:00:00+00:00", NOW, BRU),
        )
        self.assertIn("2025", timeline.humanize("2025-12-30T09:00:00+00:00", NOW, BRU))

    def test_unreadable_and_bad_zone(self):
        for bad in (None, "", "not a date"):
            self.assertEqual(timeline.humanize(bad, NOW, BRU), "")
        self.assertTrue(
            timeline.humanize(
                "2026-09-16T08:00:00+00:00", NOW, "Mars/Olympus"
            ).startswith("today at 10:00")
        )

    def test_other_zones_are_respected(self):
        self.assertTrue(
            timeline.humanize(
                "2026-09-16T08:00:00+00:00", NOW, "America/New_York"
            ).startswith("today at 04:00")
        )

    def test_due(self):
        self.assertIn(
            "still to come, in 5 hours",
            timeline.due("2026-09-16T17:00:00+00:00", NOW, BRU),
        )
        self.assertIn(
            "already passed, 19 hours ago",
            timeline.due("2026-09-15T17:00:00+00:00", NOW, BRU),
        )
        self.assertIn(
            "in 25 minutes",
            timeline.due(NOW + datetime.timedelta(minutes=25), NOW, BRU),
        )

    def test_now_note(self):
        note = timeline.now_note(NOW, BRU)
        self.assertIn("Wednesday 16 September 2026, 14:00 in Europe/Brussels", note)
        self.assertIn("the way a friend would", note)
        self.assertNotIn(
            "the way a friend would", timeline.now_note(NOW, BRU, speaking=False)
        )

    def test_render(self):
        at = timeline.stamp(NOW - datetime.timedelta(minutes=5))
        live = timeline.render(
            [
                {"role": "user", "content": "a", "at": at},
                {"role": "assistant", "content": "b", "at": at},
            ],
            NOW,
            BRU,
        )
        self.assertNotIn("Time marker", json.dumps(live))
        history = [
            {
                "role": "assistant",
                "content": "A walk at 7PM then?",
                "at": "2026-09-15T13:00:00+00:00",
            },
            {"role": "user", "content": "Top", "at": "2026-09-16T11:55:00+00:00"},
        ]
        rendered = timeline.render(history, NOW, BRU)
        self.assertTrue(
            rendered[0]["content"].startswith("[Time marker: sent yesterday at 15:00")
        )
        self.assertTrue(
            rendered[1]["content"].startswith("[Time marker: sent today at 13:55")
        )
        self.assertEqual([m["role"] for m in rendered], ["assistant", "user"])
        legacy = [{"role": "user", "content": "old"}]
        self.assertEqual(timeline.render(legacy, NOW, BRU), legacy)

    def test_helpers(self):
        self.assertEqual(timeline.after_minutes(90, NOW), "2026-09-16T13:30:00+00:00")
        for bad in (0, -5, 24 * 60 + 1, None, "soon"):
            self.assertEqual(timeline.after_minutes(bad, NOW), "")
        self.assertEqual(
            timeline.strip_markers(
                "[Time marker: sent yesterday at 15:00]\nHow did it go?"
            ),
            "How did it go?",
        )
        self.assertEqual(timeline.strip_markers("  Hi there "), "  Hi there ")
        self.assertEqual(timeline.parse("2026-09-16T12:00:00"), NOW)


SEED = {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19}


class Ocean(unittest.TestCase):
    def test_band_boundaries(self):
        for score, band in ((0, "very low"), (20, "very low"), (21, "low"), (40, "low"),
                            (41, "moderate"), (60, "moderate"), (61, "high"), (80, "high"),
                            (81, "very high"), (100, "very high")):  # fmt: skip
            self.assertEqual(ocean.band(score), band, score)

    def test_block_is_deterministic_and_named(self):
        block = ocean.render_block(SEED, name="Emma")
        self.assertEqual(block, ocean.render_block(SEED, name="Emma"))
        self.assertIn("communication style as Emma.", block)
        self.assertIn("- Openness 59/100 (moderate):", block)
        self.assertIn(
            "- Emotional stability 81/100 — Neuroticism 19/100 (very high):", block
        )
        self.assertNotIn("Persona briefing", block)
        self.assertIn(
            "## Persona briefing\n\nLoves puns.",
            ocean.render_block(SEED, briefing="Loves puns."),
        )

    def test_validation(self):
        for bad in (
            None,
            {"O": 1},
            {**SEED, "E": 101},
            {**SEED, "A": True},
            {**SEED, "C": "high"},
        ):
            with self.assertRaises(ocean.PersonaError):
                ocean.validate_ocean(bad)
        self.assertEqual(ocean.validate_ocean({**SEED, "O": 59.4})["O"], 59)


if __name__ == "__main__":
    unittest.main()
