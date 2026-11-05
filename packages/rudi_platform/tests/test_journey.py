"""
Journey format (rudi-journey@1) and the manifest fields it relies on: outcomes, on_enter,
session-scoped memory, required assets, and "*" instruction sections.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.journey import (
    CONCLUDED,
    JourneyError,
    Transition,
    check_journey,
    parse_journey,
)
from rudi_platform.skills.composer import compose, parse_composition
from rudi_platform.skills.manifest import ManifestError
from skill_fixtures import m, raw

GOAL = {
    "hooks": ["on_enter", "contribute", "on_signals"],
    "outcomes": ["accepted", "gave_up"],
    "memory": {
        "writes": {"goal": "str", "tries": "int"},
        "session": ["tries"],
    },
}
COMMIT = {
    "hooks": ["contribute", "on_signals"],
    "outcomes": ["committed"],
    "assets": ["guidance"],
}
JOURNEY = {
    "format": "rudi-journey@1",
    "start": "goal",
    "greet": {"first": "intro", "again": "welcome_back"},
    "stages": {
        "goal": {
            "skill": "goal",
            "on": {
                "accepted": {"to": "commit", "clear_history": True},
                "gave_up": CONCLUDED,
            },
        },
        "commit": {"skill": "commit", "on": {"committed": "goal"}},
    },
}


def inventory():
    return {
        "goal": [m("goal", **GOAL)],
        "commit": [m("commit", **COMMIT)],
        "voice-only": [m("voice-only", channels=["voice"])],
        "slow": [m("slow", kind="turn")],
    }


def composed(*slugs, channels=("whatsapp",)):
    comp = parse_composition(
        {
            "channels": list(channels),
            "locales": ["en"],
            "skills": [{"skill": s, "major": 1} for s in slugs],
        }
    )
    return compose(comp, inventory())


class Manifest(unittest.TestCase):
    def test_new_fields_parse(self):
        goal = m("goal", **GOAL)
        self.assertEqual(goal.outcomes, {"accepted", "gave_up"})
        self.assertEqual(goal.memory_session, {"tries"})
        self.assertIn("on_enter", goal.hooks)
        self.assertEqual(m("commit", **COMMIT).assets, {"guidance"})

    def test_bad_new_fields_are_reported_together(self):
        bad = raw(
            "x",
            hooks=["contribute"],
            outcomes=["Accepted", "accepted"],
            assets=["About Me"],
            memory={"writes": {"goal": "str"}, "session": ["tries"]},
        )
        with self.assertRaises(ManifestError) as caught:
            from rudi_platform.skills.manifest import parse_manifest

            parse_manifest(bad)
        problems = " | ".join(caught.exception.problems)
        self.assertIn("outcomes", problems)
        self.assertIn("on_signals", problems)
        self.assertIn("assets", problems)
        self.assertIn("memory.session", problems)

    def test_outcomes_belong_to_real_time_skills(self):
        with self.assertRaises(ManifestError):
            m("job", kind="background", outcomes=["done"])

    def test_any_locale_sections(self):
        spec = {"body": {"overridable": True, "locales": ["*"]}}
        skill = m("goal", prompt_sections=spec)
        comp = parse_composition(
            {
                "channels": ["whatsapp"],
                "locales": ["nl", "en"],
                "skills": [
                    {
                        "skill": "goal",
                        "major": 1,
                        "prompt_overrides": {
                            "body": {"mode": "append", "text": {"*": "Be brief."}}
                        },
                    }
                ],
            }
        )
        out = compose(comp, {"goal": [skill]})
        self.assertEqual(out.errors, ())
        self.assertEqual(out.warnings, ())  # no "has no nl text" noise for instructions
        with self.assertRaises(ManifestError):
            m("goal", prompt_sections={"body": {"locales": ["*", "en"]}})
        comp_nl = parse_composition(
            {
                "channels": ["whatsapp"],
                "locales": ["nl"],
                "skills": [
                    {
                        "skill": "goal",
                        "major": 1,
                        "prompt_overrides": {
                            "body": {"mode": "append", "text": {"nl": "Kort."}}
                        },
                    }
                ],
            }
        )
        self.assertEqual(compose(comp_nl, {"goal": [skill]}).errors, ())

    def test_composer_collects_session_memory_and_assets(self):
        out = composed("goal", "commit")
        self.assertEqual(out.session_memory, {"tries"})
        self.assertEqual(out.assets, {"guidance"})
        self.assertEqual(out.skill("goal").ref, "goal@1.0.0")
        self.assertIsNone(out.skill("nope"))


class Parse(unittest.TestCase):
    def test_parse_and_walk(self):
        j = parse_journey(JOURNEY)
        self.assertEqual(j.start, "goal")
        self.assertEqual(j.next("goal", "accepted"), Transition("commit", True))
        self.assertEqual(j.next("goal", "gave_up"), Transition(CONCLUDED))
        self.assertIsNone(j.next("goal", "unclear"))
        self.assertIsNone(j.next("goal", None))
        self.assertEqual(j.stage_skills, {"goal", "commit"})
        with self.assertRaises(JourneyError):
            j.stage("nowhere")

    def test_every_problem_is_reported(self):
        bad = {
            "format": "rudi-journey@2",
            "start": "missing",
            "greet": {"first": "media_ack"},  # needs {kind}: not a greeting
            "stages": {
                "goal": {"skill": "goal", "on": {"ok": "nowhere", "x": {"to": 1}}},
                "Bad Name": {"skill": "x"},
                CONCLUDED: {"skill": "x"},
                "orphan": {"skill": "commit"},
                "noskill": {},
                "flag": {
                    "skill": "y",
                    "on": {"a": {"to": "goal", "clear_history": "yes"}},
                },
            },
        }
        with self.assertRaises(JourneyError) as caught:
            parse_journey(bad)
        problems = " | ".join(caught.exception.problems)
        for expected in (
            "format",
            "start must name",
            "greet.first",
            "no stage 'nowhere'",
            "stages.goal.on.x",
            "stages.Bad Name",
            "stages.concluded",
            "stages.noskill",
            "clear_history must be",
        ):
            self.assertIn(expected, problems)

    def test_unreachable_stage(self):
        j = {**JOURNEY, "stages": {**JOURNEY["stages"], "spare": {"skill": "x"}}}
        with self.assertRaises(JourneyError) as caught:
            parse_journey(j)
        self.assertIn("'spare' cannot be reached", " ".join(caught.exception.problems))

    def test_not_an_object(self):
        with self.assertRaises(JourneyError):
            parse_journey([])  # type: ignore[arg-type]


class Check(unittest.TestCase):
    def test_a_fitting_journey(self):
        self.assertEqual(
            check_journey(parse_journey(JOURNEY), composed("goal", "commit")), []
        )

    def test_misfits(self):
        j = parse_journey(
            {
                "format": "rudi-journey@1",
                "start": "goal",
                "stages": {
                    "goal": {
                        "skill": "goal",
                        "on": {"done": "commit", "accepted": "two"},
                    },
                    "commit": {"skill": "absent"},
                    "two": {"skill": "goal", "on": {"accepted": "voice"}},
                    "voice": {"skill": "voice-only", "on": {"x": "slow"}},
                    "slow": {"skill": "slow"},
                },
            }
        )
        problems = " | ".join(
            check_journey(
                j,
                composed("goal", "voice-only", "slow", channels=("whatsapp", "voice")),
            )
        )
        self.assertIn("never reports 'done'", problems)
        self.assertIn("absent is not in the clone", problems)
        self.assertIn("goal already serves stage 'goal'", problems)
        self.assertIn("voice-only does not serve whatsapp", problems)
        self.assertIn("slow is not a real-time stage skill", problems)


VOICE_VARIANT = {
    **JOURNEY,
    "channels": {
        "voice": {"start": "commit", "on": {"commit": {"committed": CONCLUDED}}}
    },
}


class Variants(unittest.TestCase):
    def test_a_channel_variant_replaces_start_and_transitions(self):
        j = parse_journey(VOICE_VARIANT)
        self.assertIs(j.for_channel("whatsapp"), j)
        voice = j.for_channel("voice")
        self.assertEqual(voice.start, "commit")
        self.assertEqual(voice.next("commit", "committed"), Transition(CONCLUDED))
        self.assertEqual(voice.next("goal", "accepted"), Transition("commit", True))
        self.assertEqual(j.next("commit", "committed"), Transition("goal"))

    def test_bad_variants(self):
        bad = {
            **JOURNEY,
            "channels": {
                "pigeon": {},
                "voice": {
                    "start": "nowhere",
                    "on": {"ghost": {}, "goal": {"x": "nowhere"}},
                },
                "sms": {"extra": 1},
            },
        }
        with self.assertRaises(JourneyError) as caught:
            parse_journey(bad)
        problems = " | ".join(caught.exception.problems)
        for expected in ("channels.pigeon", "channels.voice.start", "channels.voice.on.ghost",
                         "channels.voice.on.goal.x: no stage", "channels.sms: unknown keys"):  # fmt: skip
            self.assertIn(expected, problems)

    def test_a_stage_reached_only_on_one_channel(self):
        only_voice = {
            **JOURNEY,
            "stages": {**JOURNEY["stages"], "call_only": {"skill": "voice-only"}},
            "channels": {"voice": {"start": "call_only"}},
        }
        j = parse_journey(only_voice)  # reachable on voice: not an orphan
        fits = composed("goal", "commit", "voice-only", channels=("whatsapp", "voice"))
        self.assertEqual(
            check_journey(j, fits), []
        )  # voice-only need not serve whatsapp
        also_sms = parse_journey(
            {
                **only_voice,
                "channels": {
                    "voice": {"start": "call_only"},
                    "sms": {"start": "call_only"},
                },
            }
        )
        clone = composed(
            "goal", "commit", "voice-only", channels=("whatsapp", "voice", "sms")
        )
        problems = check_journey(also_sms, clone)
        self.assertIn("voice-only does not serve sms", " ".join(problems))

    def test_variant_outcomes_must_be_reported(self):
        j = parse_journey(
            {
                **JOURNEY,
                "channels": {"voice": {"on": {"commit": {"hung_up": CONCLUDED}}}},
            }
        )
        problems = check_journey(j, composed("goal", "commit"))
        self.assertIn("never reports 'hung_up'", " ".join(problems))


class ChannelNarrowing(unittest.TestCase):
    def _compose(self, item, baseline=()):
        comp = parse_composition(
            {"channels": ["whatsapp", "voice"], "locales": ["en"], "skills": [item]}
        )
        return compose(comp, {"goal": [m("goal", **GOAL)]}, baseline)

    def test_a_skill_can_be_kept_to_some_channels(self):
        out = self._compose({"skill": "goal", "major": 1, "channels": ["whatsapp"]})
        self.assertEqual(out.per_channel, {"whatsapp": ("goal",), "voice": ()})
        self.assertEqual(out.memory_schema_for("voice"), {})
        self.assertEqual(
            out.memory_schema_for("whatsapp"), {"goal": "str", "tries": "int"}
        )
        out = self._compose(
            {"skill": "goal", "major": 1, "channels": ["voice"]}, ["goal"]
        )
        self.assertIn("baseline skill goal changed", " ".join(out.review_reasons))

    def test_narrowing_never_widens(self):
        from rudi_platform.skills.composer import CompositionError

        for bad in (["sms"], [], "whatsapp"):
            with self.assertRaises(CompositionError):
                self._compose({"skill": "goal", "major": 1, "channels": bad})


if __name__ == "__main__":
    unittest.main()
