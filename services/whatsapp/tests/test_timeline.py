"""
Time awareness — Rudi stores whole timestamps and is told them in human terms.

The bug this guards: a commitment remembered as "walk at 7PM" with no date. Rudi chased the
person for an update on an evening that had not happened yet, because nothing told him which
day that 7PM belonged to — or what day it was now.

Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import os
import sys
import json
import types
import datetime
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from fake_s3 import FakeS3  # noqa: E402

_FAKE_S3 = FakeS3()
# Only install a boto3 double if nothing else already did — replacing a sibling module's stub
# is how a green file makes a red suite.
if "boto3" not in sys.modules:
    _boto3 = types.ModuleType("boto3")
    _boto3.client = lambda name, *a, **k: _FAKE_S3
    sys.modules["boto3"] = _boto3
os.environ.setdefault("DATA_BUCKET", "meetrudi-ai-data-test")

import timeline   # noqa: E402
import responder  # noqa: E402

UTC = datetime.timezone.utc
BRU = "Europe/Brussels"          # UTC+2 throughout September 2026


def utc(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


# Wednesday 16 September 2026, 14:00 in Brussels.
NOW = utc("2026-09-16T12:00:00")


# --------------------------------------------------------------------------- the helper itself
class Humanize(unittest.TestCase):
    def test_same_day_is_today(self):
        self.assertTrue(timeline.humanize("2026-09-16T08:00:00+00:00", NOW, BRU)
                        .startswith("today at 10:00"))

    def test_the_day_before_is_yesterday(self):
        self.assertTrue(timeline.humanize("2026-09-15T17:00:00+00:00", NOW, BRU)
                        .startswith("yesterday at 19:00"))

    def test_the_day_after_is_tomorrow(self):
        self.assertTrue(timeline.humanize("2026-09-17T06:30:00+00:00", NOW, BRU)
                        .startswith("tomorrow at 08:30"))

    def test_this_week_uses_the_weekday(self):
        self.assertTrue(timeline.humanize("2026-09-13T09:00:00+00:00", NOW, BRU)
                        .startswith("last Sunday at 11:00"))
        self.assertTrue(timeline.humanize("2026-09-19T09:00:00+00:00", NOW, BRU)
                        .startswith("this coming Saturday at 11:00"))

    def test_a_long_pause_counts_the_days(self):
        self.assertTrue(timeline.humanize("2026-09-02T09:00:00+00:00", NOW, BRU)
                        .startswith("14 days ago at 11:00"))

    def test_the_date_is_always_kept_alongside(self):
        """The relative half is what Rudi says; the date is what lets a week's pause make sense."""
        self.assertIn("(Tuesday 15 September)",
                      timeline.humanize("2026-09-15T17:00:00+00:00", NOW, BRU))

    def test_another_year_says_so(self):
        self.assertIn("2025", timeline.humanize("2025-12-30T09:00:00+00:00", NOW, BRU))

    def test_the_local_day_decides_not_the_utc_day(self):
        """23:30 UTC on the 15th is already the 16th in Brussels — so it is today, not yesterday."""
        self.assertTrue(timeline.humanize("2026-09-15T23:30:00+00:00", NOW, BRU)
                        .startswith("today at 01:30"))

    def test_unreadable_is_empty_not_an_error(self):
        for bad in (None, "", "not a date"):
            self.assertEqual(timeline.humanize(bad, NOW, BRU), "")

    def test_a_bad_timezone_falls_back_to_the_pilot_default(self):
        self.assertTrue(timeline.humanize("2026-09-16T08:00:00+00:00", NOW, "Mars/Olympus")
                        .startswith("today at 10:00"))


class Due(unittest.TestCase):
    def test_still_ahead(self):
        text = timeline.due("2026-09-16T17:00:00+00:00", NOW, BRU)
        self.assertIn("today at 19:00", text)
        self.assertIn("still to come, in 5 hours", text)

    def test_already_passed(self):
        text = timeline.due("2026-09-15T17:00:00+00:00", NOW, BRU)
        self.assertIn("yesterday at 19:00", text)
        self.assertIn("already passed, 19 hours ago", text)

    def test_minutes_when_close(self):
        self.assertIn("in 25 minutes", timeline.due(NOW + datetime.timedelta(minutes=25), NOW, BRU))


class NowNote(unittest.TestCase):
    def test_carries_the_full_local_moment(self):
        note = timeline.now_note(NOW, BRU)
        self.assertIn("Wednesday 16 September 2026, 14:00 in Europe/Brussels", note)
        self.assertIn("the way a friend would", note)

    def test_the_stored_variant_leaves_out_speaking_guidance(self):
        self.assertNotIn("the way a friend would", timeline.now_note(NOW, BRU, speaking=False))


class Render(unittest.TestCase):
    def test_timestamps_never_reach_the_provider(self):
        rendered = timeline.render([{"role": "user", "content": "hi", "at": timeline.stamp(NOW)}],
                                   NOW, BRU)
        self.assertEqual(rendered, [{"role": "user", "content": "hi"}])

    def test_no_marker_inside_a_live_exchange(self):
        at = timeline.stamp(NOW - datetime.timedelta(minutes=5))
        rendered = timeline.render([{"role": "user", "content": "a", "at": at},
                                    {"role": "assistant", "content": "b", "at": at}], NOW, BRU)
        self.assertNotIn("Time marker", json.dumps(rendered))

    def test_a_marker_where_the_conversation_picks_up_again(self):
        history = [
            {"role": "assistant", "content": "A walk at 7PM then?", "at": "2026-09-15T13:00:00+00:00"},
            {"role": "user", "content": "Top", "at": "2026-09-16T11:55:00+00:00"},
        ]
        rendered = timeline.render(history, NOW, BRU)
        self.assertTrue(rendered[0]["content"].startswith("[Time marker: sent yesterday at 15:00"))
        self.assertTrue(rendered[1]["content"].startswith("[Time marker: sent today at 13:55"))
        self.assertEqual([m["role"] for m in rendered], ["assistant", "user"],
                         "markers ride inside the text; no system message is inserted between turns")

    def test_crossing_midnight_is_marked_even_without_a_long_gap(self):
        history = [{"role": "user", "content": "night", "at": "2026-09-15T21:30:00+00:00"},
                   {"role": "user", "content": "morning", "at": "2026-09-15T22:10:00+00:00"}]
        rendered = timeline.render(history, NOW, BRU)
        self.assertIn("today at 00:10", rendered[1]["content"])

    def test_history_from_before_timestamps_passes_through(self):
        legacy = [{"role": "user", "content": "old"}]
        self.assertEqual(timeline.render(legacy, NOW, BRU), legacy)


class Helpers(unittest.TestCase):
    def test_after_minutes_respects_the_24h_limit(self):
        self.assertEqual(timeline.after_minutes(90, NOW), "2026-09-16T13:30:00+00:00")
        for bad in (0, -5, 24 * 60 + 1, None, "soon"):
            self.assertEqual(timeline.after_minutes(bad, NOW), "")

    def test_an_echoed_marker_is_removed(self):
        self.assertEqual(timeline.strip_markers("[Time marker: sent yesterday at 15:00]\nHow did it go?"),
                         "How did it go?")

    def test_clean_text_is_left_exactly_alone(self):
        self.assertEqual(timeline.strip_markers("  Hi there "), "  Hi there ")

    def test_a_naive_timestamp_is_read_as_utc(self):
        self.assertEqual(timeline.parse("2026-09-16T12:00:00"), NOW)


# --------------------------------------------------------------------------- inside the responder
class ResponderClock(unittest.TestCase):
    """The reported bug, end to end through respond() — both directions."""

    def setUp(self):
        self._s3, self._generate = responder.s3, responder.gateway.generate
        responder.s3 = _FAKE_S3
        responder._asset_cache.clear()
        for key in ("prompts/rudi_guardrails.md", "prompts/rudi_learn_prompt_wa.md",
                    "prompts/rudi_goal_prompt.md", "prompts/rudi_commit_prompt.md",
                    "contexts/rudi-context.md", "contexts/diabetes-t2d-guidance.md"):
            _FAKE_S3.put_object(Bucket=responder.DATA_BUCKET, Key=key, Body=("PROMPT " + key).encode())
        self.seen = {}
        self.reply = {"reply": "ok", "signals": {}}

        def generate(messages, json_mode=False):
            self.seen["messages"] = messages
            return {"text": json.dumps(self.reply), "model": "fake"}

        responder.gateway.generate = generate

    def tearDown(self):
        responder.s3, responder.gateway.generate = self._s3, self._generate
        responder._asset_cache.clear()

    def _committed(self, agreed_at, check_in_at):
        return {"phase": "committed", "session_id": 3, "goal": "walk more", "goal_domain": "fitness",
                "commitment": "a walk at 7PM", "commitment_agreed_at": agreed_at,
                "check_in_at": check_in_at, "clarifiers_used": 0, "commit_attempts": 0,
                "reject_count": 0,
                "history": [{"role": "assistant", "content": "Deal — a walk at 7PM.",
                             "at": agreed_at}]}

    def _system(self):
        return self.seen["messages"][0]["content"]

    def test_yesterdays_7pm_is_presented_as_passed(self):
        state = self._committed("2026-09-15T13:00:00+00:00", "2026-09-15T17:30:00+00:00")
        responder.respond(state, "Hoi", tz=BRU, now=NOW)
        system = self._system()
        self.assertIn("16 September 2026, 14:00 in Europe/Brussels", system)
        self.assertIn("They agreed yesterday at 15:00", system)
        self.assertIn("yesterday at 19:30", system)
        self.assertIn("already passed", system)

    def test_todays_7pm_is_presented_as_still_to_come(self):
        """The reported case: Rudi treated a 7PM that had not arrived yet as already missed."""
        state = self._committed("2026-09-16T08:00:00+00:00", "2026-09-16T17:30:00+00:00")
        responder.respond(state, "Hoi", tz=BRU, now=NOW)
        system = self._system()
        self.assertIn("They agreed today at 10:00", system)
        # The [Now] line names "already passed" as an example label, so look at the check-in
        # sentence itself rather than the whole prompt.
        self.assertIn("You planned to check in on it today at 19:30 (Wednesday 16 September) "
                      "— still to come", system)
        self.assertNotIn("today at 19:30 (Wednesday 16 September) — already passed", system)

    def test_the_model_gets_markers_but_never_raw_timestamps(self):
        state = self._committed("2026-09-16T08:00:00+00:00", "2026-09-16T17:30:00+00:00")
        responder.respond(state, "Hoi", tz=BRU, now=NOW)
        history = self.seen["messages"][1:]
        self.assertTrue(all(set(m) == {"role", "content"} for m in history))
        self.assertTrue(history[0]["content"].startswith("[Time marker: sent today at 10:00"))

    def test_every_remembered_turn_is_stamped_in_full(self):
        state = self._committed("2026-09-16T08:00:00+00:00", None)
        _, new_state, _ = responder.respond(state, "Hoi", tz=BRU, now=NOW)
        self.assertEqual([m["at"] for m in new_state["history"][-2:]], [timeline.stamp(NOW)] * 2)

    def test_a_commitment_and_its_check_in_are_stored_as_moments(self):
        state = dict(self._committed(None, None), phase="commit", commitment=None, history=[])
        self.reply = {"reply": "Great!", "signals": {"commitment_made": True,
                                                      "check_in_about": "the 7PM walk",
                                                      "check_in_minutes": 330}}
        _, new_state, _ = responder.respond(state, "Ok, 7PM walk", tz=BRU, now=NOW)
        self.assertEqual(new_state["commitment_agreed_at"], timeline.stamp(NOW))
        self.assertEqual(new_state["check_in_at"], "2026-09-16T17:30:00+00:00")

    def test_an_echoed_marker_never_reaches_the_person(self):
        state = self._committed("2026-09-16T08:00:00+00:00", None)
        self.reply = {"reply": "[Time marker: sent today at 10:00]\nGood luck tonight!", "signals": {}}
        reply, _, _ = responder.respond(state, "Hoi", tz=BRU, now=NOW)
        self.assertEqual(reply, "Good luck tonight!")

    def test_a_reach_out_knows_when_its_check_in_was_planned(self):
        state = self._committed("2026-09-16T08:00:00+00:00", None)
        responder.reach_out(state, "nl", commitment="the 7PM walk",
                            commitment_at="2026-09-16T17:30:00+00:00", tz=BRU, now=NOW)
        system = self._system()
        self.assertIn("They agreed to it today at 10:00", system)
        self.assertIn("The check-in was planned for today at 19:30", system)
        self.assertIn("[Now: it is", system)

    def test_a_summary_keeps_dates_instead_of_saying_tonight(self):
        responder.summarize([{"role": "user", "content": "walk at 7PM", "at": timeline.stamp(NOW)}],
                            tz=BRU, now=NOW)
        system = self._system()
        self.assertIn("never \"today\", \"tonight\" or \"tomorrow\"", system)
        self.assertIn("16 September 2026", system)
        self.assertNotIn("the way a friend would", system)


if __name__ == "__main__":
    unittest.main()
