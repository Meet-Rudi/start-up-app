"""
Skill manifests and the composer — what a clone may be built from, checked at publish.

What this guards: a manifest that half-works, a clone pinned to a version nobody released, a
slow skill on a voice call, an override of something the skill author locked, two skills
disagreeing about a memory field, and a baseline guard removed without review.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))

from rudi_platform.skills.composer import CompositionError, compose, parse_composition
from rudi_platform.skills.contract import SkillKind
from rudi_platform.skills.manifest import ManifestError, parse_manifest
from skill_fixtures import inventory, m, raw


def comp(skills, channels=("whatsapp", "voice"), locales=("nl", "en")):
    return parse_composition(
        {"channels": list(channels), "locales": list(locales), "skills": skills}
    )


def pick(slug, major=1, **kw):
    return {"skill": slug, "major": major, **kw}


BASE = [
    pick("journey-engine"),
    pick("goal-setting"),
    pick("profile-summary"),
    pick("crisis-signposting"),
]


class Manifests(unittest.TestCase):
    def test_each_kind_parses(self):
        self.assertIs(m("a").kind, SkillKind.REAL_TIME)
        self.assertEqual(
            m("a").latency, "voice"
        )  # real-time defaults to the fastest class
        self.assertIs(m("b", kind="turn").kind, SkillKind.TURN)
        bg = m("c", kind="background")
        self.assertIsNone(bg.latency)
        self.assertEqual(str(m("d", "2.3.4").ref), "d@2.3.4")

    def test_every_problem_is_reported_at_once(self):
        with self.assertRaises(ManifestError) as caught:
            parse_manifest(
                raw("Bad Slug", "1.0", kind="turn", tool=None, channels=["fax"])
            )
        problems = " | ".join(caught.exception.problems)
        for expected in ("slug", "version", "channels", "tool.name"):
            self.assertIn(expected, problems)
        self.assertGreaterEqual(len(caught.exception.problems), 4)

    def test_kind_rules(self):
        cases = [
            raw("a", hooks=[]),  # real-time without hooks
            raw(
                "a", tool={"name": "x", "description": "y", "input_schema": {}}
            ),  # real-time + tool
            raw("b", kind="turn", latency=None),  # turn without latency
            raw("b", kind="turn", hooks=["contribute"]),  # turn with hooks
            raw("c", kind="background", triggers=[]),  # background without triggers
            raw("c", kind="background", latency="voice"),  # background with latency
            raw("d", latency="instant"),
            raw("d", model_tier="genius"),
            raw("d", requires=["d@1"]),
            raw("d", requires=["other"]),
            raw("d", data_classes=["biometric"]),
            raw("d", format="rudi-skill@2"),
        ]
        for manifest in cases:
            with self.subTest(manifest=manifest), self.assertRaises(ManifestError):
                parse_manifest(manifest)

    def test_settings_and_sections(self):
        with self.assertRaises(ManifestError):
            parse_manifest(raw("a", settings={"n": {"type": "int", "default": "two"}}))
        with self.assertRaises(ManifestError):
            parse_manifest(raw("a", settings={"t": {"type": "enum", "default": "x"}}))
        with self.assertRaises(ManifestError):
            parse_manifest(
                raw("a", settings={"n": {"type": "int", "default": 9, "max": 5}})
            )
        with self.assertRaises(ManifestError):
            parse_manifest(raw("a", prompt_sections={"rules": {"locales": []}}))
        with self.assertRaises(ManifestError):
            parse_manifest(raw("a", memory={"writes": {"goal": "tuple"}}))
        ok = m(
            "a", settings={"n": {"type": "float", "default": 0.5, "min": 0, "max": 1}}
        )
        self.assertIsNone(ok.settings["n"].problem(1))
        self.assertIsNotNone(ok.settings["n"].problem(True))


class Compose(unittest.TestCase):
    def setUp(self):
        self.inv = inventory()

    def test_happy_path(self):
        skills = [
            pick("journey-engine"),
            pick("goal-setting", settings={"max_clarifiers": 1, "tone": "brisk"}),
            pick("profile-summary"),
            pick("crisis-signposting"),
        ]
        out = compose(comp(skills), self.inv, baseline=["crisis-signposting"])
        self.assertTrue(out.ok, out.errors)
        self.assertEqual(
            out.pins()["goal-setting"], "goal-setting@1.2.0"
        )  # newest minor of major 1
        goal = next(s for s in out.skills if s.manifest.slug == "goal-setting")
        self.assertEqual(
            goal.settings,
            {"max_clarifiers": 1, "domain_list": ["fitness", "sleep"], "tone": "brisk"},
        )
        self.assertEqual(
            dict(out.memory_schema),
            {"goal": "str", "goal_domain": "str", "summary": "str"},
        )
        self.assertEqual(
            out.per_channel["voice"],
            ("journey-engine", "goal-setting", "profile-summary", "crisis-signposting"),
        )
        self.assertEqual(out.markers, ("[runtime:", "goal_status"))
        self.assertEqual(out.review_reasons, ())
        self.assertTrue(any("'examples' has no nl text" in w for w in out.warnings))

    def test_a_major_pin_never_jumps_majors(self):
        out = compose(comp([pick("goal-setting", 2)]), self.inv)
        self.assertEqual(out.pins()["goal-setting"], "goal-setting@2.0.0")
        self.assertFalse(compose(comp([pick("goal-setting", 3)]), self.inv).ok)

    def _errors(self, skills, **kw):
        out = compose(comp(skills, **kw), self.inv)
        self.assertFalse(out.ok)
        return " | ".join(out.errors)

    def test_errors(self):
        cases = {
            "no released version": [pick("teleport")],
            "twice": [*BASE, pick("goal-setting")],
            "requires journey-engine@1": [pick("goal-setting")],
            "conflicts with knowledge-retrieval": [
                pick("booking"),
                pick("knowledge-retrieval"),
            ],
            "too slow for voice": [pick("knowledge-retrieval")],
            "unknown setting": [
                *BASE[:1],
                pick("goal-setting", settings={"colour": "red"}),
            ],
            "not overridable": [
                *BASE[:1],
                pick("goal-setting", settings={"domain_list": ["x"]}),
            ],
            "at most 5": [
                *BASE[:1],
                pick("goal-setting", settings={"max_clarifiers": 9}),
            ],
            "'rules' is not overridable": [
                *BASE[:1],
                pick(
                    "goal-setting",
                    prompt_overrides={"rules": {"mode": "append", "text": {"nl": "x"}}},
                ),
            ],
            "'intro' does not exist": [
                *BASE[:1],
                pick(
                    "goal-setting",
                    prompt_overrides={"intro": {"mode": "append", "text": {"nl": "x"}}},
                ),
            ],
            "mode must be": [
                *BASE[:1],
                pick(
                    "goal-setting",
                    prompt_overrides={
                        "examples": {"mode": "merge", "text": {"nl": "x"}}
                    },
                ),
            ],
            "not one of the clone's languages": [
                *BASE[:1],
                pick(
                    "goal-setting",
                    prompt_overrides={
                        "examples": {"mode": "replace", "text": {"fr": "x"}}
                    },
                ),
            ],
            "memory field 'goal'": [*BASE, pick("goal-tracker")],
        }
        for expected, skills in cases.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, self._errors(skills))

    def test_a_skill_on_none_of_the_channels(self):
        self.inv["sms-only"] = [m("sms-only", channels=["sms"])]
        self.assertIn(
            "serves none", self._errors([pick("sms-only")], channels=("whatsapp",))
        )

    def test_an_override_supplies_a_missing_translation(self):
        skills = [
            pick("journey-engine"),
            pick(
                "goal-setting",
                prompt_overrides={
                    "examples": {"mode": "replace", "text": {"nl": "Voorbeeld"}}
                },
            ),
        ]
        out = compose(comp(skills), self.inv)
        self.assertTrue(out.ok, out.errors)
        self.assertFalse(any("examples" in w for w in out.warnings))

    def test_review_reasons(self):
        skills = [pick("journey-engine"), pick("goal-setting"), pick("booking")]
        out = compose(
            comp(skills, channels=("whatsapp",)),
            self.inv,
            baseline=["crisis-signposting"],
        )
        reasons = " | ".join(out.review_reasons)
        self.assertIn("crisis-signposting removed", reasons)
        self.assertIn("health data", reasons)
        self.assertIn("booking calls a tenant webhook", reasons)
        changed = compose(
            comp([*BASE[:3], pick("crisis-signposting", settings={})]),
            self.inv,
            baseline=["crisis-signposting"],
        )
        self.assertEqual(changed.review_reasons, ())

    def test_unwritten_reads_warn(self):
        out = compose(comp([pick("profile-summary")]), self.inv)
        self.assertTrue(out.ok)
        self.assertTrue(any("reads 'goal'" in w for w in out.warnings))
        self.assertFalse(
            any("'locale'" in w for w in out.warnings)
        )  # platform-provided

    def test_malformed_compositions(self):
        for bad in (
            {"channels": [], "locales": ["en"], "skills": [pick("a")]},
            {"channels": ["fax"], "locales": ["en"], "skills": [pick("a")]},
            {"channels": ["api"], "locales": [], "skills": [pick("a")]},
            {"channels": ["api"], "locales": ["en"], "skills": []},
            {
                "channels": ["api"],
                "locales": ["en"],
                "skills": [{"skill": "Bad", "major": 1}],
            },
            {
                "channels": ["api"],
                "locales": ["en"],
                "skills": [{"skill": "a", "major": "1"}],
            },
            {
                "channels": ["api"],
                "locales": ["en"],
                "skills": [{"skill": "a", "major": 1, "settings": []}],
            },
        ):
            with self.subTest(bad=bad), self.assertRaises(CompositionError):
                parse_composition(bad)
        self.assertEqual(
            comp([pick("a")], locales=("nl-BE", "nl", "EN")).locales, ("nl", "en")
        )


if __name__ == "__main__":
    unittest.main()
