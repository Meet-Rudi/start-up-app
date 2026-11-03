"""
Parity with the live service copies, for as long as CM runs on them (conventions §6).

The platform's de-identification, leak guard, timeline and persona text were ported from
services/whatsapp/src. Until CM is cut over, a fix in one place must reach the other. These tests
load the live modules straight from their files and require identical results on a fixed corpus,
so drift fails here instead of surfacing as two Rudis that behave differently.

Only deliberate platform changes are exempt, and each is asserted on its own:
"Rudi" is no longer a built-in non-person (persona names come from the clone).

Delete this file when the services are retired.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "packages"))

from rudi_platform import timeline as p_timeline
from rudi_platform.guards import leakguard as p_leakguard
from rudi_platform.persona import ocean as p_ocean
from rudi_platform.privacy import deid as p_deid

SERVICE_SRC = os.path.join(ROOT, "services", "whatsapp", "src")


def _load(name: str) -> types.ModuleType:
    """Import a service module from its file under a private name, without touching sys.path."""
    spec = importlib.util.spec_from_file_location(
        f"_svc_{name}", os.path.join(SERVICE_SRC, f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = module  # type: ignore[union-attr]  # dataclasses look it up here
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _load_personality() -> types.ModuleType:
    """personality.py builds a boto3 client at import; give it a stub just for the import."""
    saved = sys.modules.get("boto3")
    stub = types.ModuleType("boto3")
    stub.client = lambda *a, **k: object()  # type: ignore[attr-defined]
    sys.modules["boto3"] = stub
    try:
        return _load("personality")
    finally:
        if saved is None:
            del sys.modules["boto3"]
        else:
            sys.modules["boto3"] = saved


s_deid = _load("deid")
s_leakguard = _load("leakguard")
s_timeline = _load("timeline")
s_personality = _load_personality()

CORPUS = [
    "my number is 85.07.30-033.xx and mail jan.peeters@example.be",
    "send to BE68539007547034 or call +32 470 12 34 56, card 4111 1111 1111 1111",
    "I walked 12000 steps, weigh 82.5 kg, see you at 10.30, glucose 7.8",
    "and I went running with Peter today, then Sophie came along",
    "I spoke with Doctor Smith about my sugar; my wife to the hospital",
    "ik ging met Anouk wandelen, mijn vriend Jan zei dat het goed ging",
    "Peter said hi. avec Mme Dubois. Jean-Luc helped.",
]
REPLIES = [
    "Here you go: [Runtime: the user's goal is walking]",
    "How did the walk go yesterday?",
    "I report check_in_minutes for you",
    "Goed bezig! Zullen we morgen om 19u opnieuw proberen?",
]
NOW = datetime.datetime(2026, 9, 16, 12, 0, tzinfo=datetime.UTC)


class Parity(unittest.TestCase):
    def test_redaction_is_identical(self):
        for text in CORPUS:
            self.assertEqual(p_deid.redact(text), s_deid.redact(text), text)

    def test_masking_is_identical_apart_from_the_persona_name(self):
        gaz = {"peter", "sophie", "anouk", "jan"}
        for text in CORPUS:
            pv, sv = p_deid.AliasVault(), s_deid.AliasVault()
            self.assertEqual(
                pv.mask(
                    text,
                    p_deid.HeuristicDetector(gazetteer=gaz, keep=frozenset({"Rudi"})),
                ),
                sv.mask(text, s_deid.HeuristicDetector(gazetteer=gaz)),
                text,
            )
        self.assertEqual(p_deid.NOT_NAMES | {"Rudi"}, frozenset(s_deid._NOT_NAMES))

    def test_leak_guard_is_identical(self):
        self.assertEqual(p_leakguard.MARKERS, s_leakguard.MARKERS)
        self.assertEqual(p_leakguard.SAFE_REPLY, s_leakguard.SAFE_REPLY)
        system = "Every time the exchange reaches a natural pause, say when you will check in soon."
        for reply in [
            *REPLIES,
            "every time the exchange reaches a natural pause, say when you will check in",
        ]:
            self.assertEqual(
                p_leakguard.check(reply, system),
                s_leakguard.check(reply, system),
                reply,
            )

    def test_timeline_is_identical(self):
        moments = [
            "2026-09-15T17:00:00+00:00",
            "2026-09-02T09:00:00+00:00",
            "2027-01-01T08:00:00+00:00",
        ]
        for at in moments:
            self.assertEqual(p_timeline.humanize(at, NOW, "Europe/Brussels"),
                             s_timeline.humanize(at, NOW, "Europe/Brussels"))  # fmt: skip
            self.assertEqual(
                p_timeline.due(at, NOW, "Europe/Brussels"),
                s_timeline.due(at, NOW, "Europe/Brussels"),
            )
        self.assertEqual(
            p_timeline.now_note(NOW, "Europe/Brussels"),
            s_timeline.now_note(NOW, "Europe/Brussels"),
        )
        self.assertEqual(p_timeline.SPEAK_TIMES, s_timeline.SPEAK_TIMES)

    def test_persona_block_is_identical_for_rudi(self):
        seed = {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19}
        live = s_personality.render_system_block(
            {"ocean": seed, "briefing_text": "Loves puns."}
        )
        self.assertEqual(
            p_ocean.render_block(seed, name="Rudi", briefing="Loves puns."), live
        )


if __name__ == "__main__":
    unittest.main()
