"""
Parity: CM's calls on the platform engine behave exactly like CM's live call brain.

The live path is services/call/src/ws.py around brain.py: on pick-up `_model_config` (the
person's name becomes an alias, topic and notes are scrubbed) → `brain.open_call`; on every
utterance `_scrub_inbound` → `_model_config` → `brain.turn`; the reply is spoken after
`_restore_outbound`. Those functions are loaded from their files with only boto3 and the model
gateway replaced. The platform side is CM's clone (rudi_tenants.cm) on the voice channel through
`engine.voice.open_call` / `call_turn`. Both get the same scripted model output.

Every step must agree on: the system prompt (line endings and blank-line runs aside, as for
WhatsApp), every other message sent to the model, the de-identified transcript, the alias vault,
the reply as stored and as spoken, phase, signals, whether the call ended, and the call state.

The call state is compared on the live brain's keys. The platform's call state also carries the
session number and, once they commit, what they committed to and when (the WhatsApp skills
record it); the live brain does not store those. Every other platform-only field must hold its
empty default.

Run:  python -m unittest discover -s packages/rudi_platform/tests -v
"""

from __future__ import annotations

import datetime
import importlib
import io
import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "packages"))

import rudi_skills
from rudi_platform.engine import (
    DictAssetStore,
    TurnEngine,
    canonical,
    load_clone,
    voice,
)
from rudi_platform.gateway import ModelEndpoint, ModelGateway, StaticSecrets
from rudi_platform.memory import to_flat
from rudi_platform.memory.state import default_for
from rudi_platform.privacy.deid import AliasVault
from rudi_platform.registry import PLATFORM, InMemoryRegistryStore
from rudi_tenants import cm

CALL_SRC = os.path.join(ROOT, "services", "call", "src")
BUCKET = "voice-parity-bucket"
GUIDANCE = "GUIDANCE: a short walk after meals helps; keep it gentle.\r\n"
# Calls prefer this document (the live bucket holds both; WhatsApp reads the other one).
CALL_GUIDANCE = "CALL GUIDANCE: lifestyle support only; small steps.\r\n"
PLATFORM_ONLY = {"session_id", "commitment", "commitment_agreed_at"}


def _seed(rel: str) -> bytes:
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read()


# The live call brain reads these keys; the guidance key is the one the live bucket holds today.
ASSET_BYTES = {
    "prompts/rudi_guardrails.md": _seed(
        "services/rudi-chat/seed/prompts/rudi_guardrails.md"
    ),
    "prompts/rudi_learn_prompt_call.md": _seed(
        "services/whatsapp/seed/prompts/rudi_learn_prompt_call.md"
    ),
    "prompts/rudi_goal_prompt.md": _seed(
        "services/rudi-chat/seed/prompts/rudi_goal_prompt.md"
    ),
    "prompts/rudi_commit_prompt.md": _seed(
        "services/rudi-chat/seed/prompts/rudi_commit_prompt.md"
    ),
    cm.ASSETS["about"]: _seed("services/ask-ai/seed/contexts/rudi-context.md"),
    cm.ASSETS["health_guidance"]: GUIDANCE.encode(),
    cm.ASSETS["health_guidance@voice"]: CALL_GUIDANCE.encode(),
}


class FakeS3:
    def get_object(self, Bucket, Key):
        if Key not in ASSET_BYTES:
            raise KeyError(f"NoSuchKey {Key}")
        return {"Body": io.BytesIO(ASSET_BYTES[Key])}

    def put_object(self, **kw):
        raise AssertionError("the parity run must not write anything")


class Script:
    def __init__(self):
        self.text: str | None = None
        self.legacy_sent: list | None = None
        self.platform_sent: list | None = None

    def take(self) -> str:
        if self.text is None:
            raise AssertionError(
                "a model call happened on a turn that should not call one"
            )
        return self.text


SCRIPT = Script()


def _load_legacy() -> types.SimpleNamespace:
    names = ["boto3", "gateway", "brain", "calllog", "deid", "relay", "timeline",
             "leakguard", "ws"]  # fmt: skip
    saved_modules = {n: sys.modules.get(n) for n in names}
    saved_path = list(sys.path)
    saved_env = os.environ.get("DATA_BUCKET")

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *a, **k: FakeS3()  # type: ignore[attr-defined]
    gateway = types.ModuleType("gateway")

    class AIError(Exception):
        pass

    class AllRateLimited(AIError):
        pass

    def generate(messages, json_mode=False, budget_s=None):
        SCRIPT.legacy_sent = [dict(m) for m in messages]
        return {"text": SCRIPT.take(), "model": "fake-model"}

    gateway.AIError, gateway.AllRateLimited, gateway.generate = (
        AIError,
        AllRateLimited,
        generate,
    )  # type: ignore[attr-defined]
    for n in names:
        sys.modules.pop(n, None)
    sys.modules["boto3"], sys.modules["gateway"] = boto3, gateway
    os.environ["DATA_BUCKET"] = BUCKET
    sys.path.insert(0, CALL_SRC)
    try:
        ws = importlib.import_module("ws")
        return types.SimpleNamespace(ws=ws, brain=sys.modules["brain"])
    finally:
        sys.path[:] = saved_path
        for n, module in saved_modules.items():
            if module is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = module
        if saved_env is None:
            os.environ.pop("DATA_BUCKET", None)
        else:
            os.environ["DATA_BUCKET"] = saved_env


LEGACY = _load_legacy()


class ScriptedTransport:
    def post_json(self, url, headers, payload, timeout):
        SCRIPT.platform_sent = [dict(m) for m in payload["messages"]]
        return {
            "choices": [
                {"message": {"content": SCRIPT.take()}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


def _engine() -> TurnEngine:
    store = InMemoryRegistryStore()
    seeded = cm.seed(store, rudi_skills.manifests())
    assets = DictAssetStore({k: v.decode("utf-8") for k, v in ASSET_BYTES.items()})
    clone = load_clone(
        store, PLATFORM, seeded.version_id, rudi_skills.runtime(), assets
    )
    endpoint = ModelEndpoint(
        name="fake", url="https://fake.example/v1/chat/completions", model="fake-model",
        secret="meetrudi/test/fake", region="eu", dpa=True,
    )  # fmt: skip
    gateway = ModelGateway(
        ScriptedTransport(), StaticSecrets({"meetrudi/test/fake": "k"})
    )
    return TurnEngine(clone, gateway, [endpoint])


def env(reply: str, **signals) -> str:
    return json.dumps({"reply": reply, "signals": signals})


START = datetime.datetime(2026, 11, 3, 16, 0, tzinfo=datetime.UTC)


class Call:
    """One call, played through the live ws/brain path and the platform side by side."""

    def __init__(self, case: unittest.TestCase, config: dict):
        self.case, self.config = case, config
        self.engine = _engine()
        self.schema = self.engine.clone.memory_schema("voice")
        self.manifest = {"call_id": "call_parity", "config": config}
        self.vault = AliasVault()
        self.steps = 0

    def _compare_prompts(self, where):
        c = self.case
        c.assertEqual(SCRIPT.legacy_sent is None, SCRIPT.platform_sent is None, where)
        if SCRIPT.legacy_sent is None:
            return
        c.assertEqual(
            canonical(SCRIPT.legacy_sent[0]["content"]),
            canonical(SCRIPT.platform_sent[0]["content"]),
            where,
        )
        c.assertEqual(SCRIPT.legacy_sent[1:], SCRIPT.platform_sent[1:], where)
        self.system = SCRIPT.platform_sent[0]["content"]

    def _compare_state(self, live: dict, mine, where):
        flat = to_flat(mine, self.schema)
        c = self.case
        for key, value in live.items():
            c.assertEqual(value, flat.get(key), f"{where}: state[{key!r}]")
        for key in set(flat) - set(live) - PLATFORM_ONLY:
            c.assertEqual(
                flat[key], default_for(self.schema.get(key, "")), f"{where}: {key}"
            )

    def pick_up(self, model: str, minutes: float = 0):
        self.steps += 1
        where = f"opening (step {self.steps})"
        now = START + datetime.timedelta(minutes=minutes)
        SCRIPT.text, SCRIPT.legacy_sent, SCRIPT.platform_sent = model, None, None
        ws = LEGACY.ws
        reply, state, info = LEGACY.brain.open_call(
            ws._model_config(self.manifest, self.config), now=now
        )
        spoken = ws._restore_outbound(self.manifest, reply)
        out = voice.open_call(self.engine, self.config, self.vault, now=now)
        self._compare_prompts(where)
        c = self.case
        c.assertEqual(reply, out.reply_masked, where)
        c.assertEqual(spoken, out.reply, where)
        c.assertEqual(info["phase"], out.stage, where)
        c.assertEqual(self.manifest.get("_vault"), self.vault.to_dict(), where)
        self._compare_state(state, out.state, where)
        self.live_state, self.state = state, out.state
        return out

    def hear(self, said: str, model: str | None, elapsed_s: int, minutes: float):
        self.steps += 1
        where = f"step {self.steps}: {said!r}"
        now = START + datetime.timedelta(minutes=minutes)
        SCRIPT.text, SCRIPT.legacy_sent, SCRIPT.platform_sent = model, None, None
        ws = LEGACY.ws
        transcript = ws._scrub_inbound(
            self.manifest, said, self.config.get("language", "en")
        )
        reply, state, info = LEGACY.brain.turn(
            self.live_state, transcript, ws._model_config(self.manifest, self.config),
            elapsed_s=elapsed_s, now=now,
        )  # fmt: skip
        spoken = ws._restore_outbound(self.manifest, reply)
        out = voice.call_turn(
            self.engine, self.state, said, self.config, self.vault,
            elapsed_s=elapsed_s, now=now,
        )  # fmt: skip
        self._compare_prompts(where)
        c = self.case
        c.assertEqual(transcript, out.masked_inbound, where)
        c.assertEqual(reply, out.reply_masked, where)
        c.assertEqual(spoken, out.reply, where)
        c.assertEqual(info["phase"], out.stage, where)
        c.assertEqual(info["signals"], out.signals, where)
        c.assertEqual(bool(info.get("ended")), out.ended, where)
        c.assertEqual(self.manifest.get("_vault"), self.vault.to_dict(), where)
        self._compare_state(state, out.state, where)
        self.live_state, self.state = state, out.state
        return out


class VoiceParity(unittest.TestCase):
    def test_a_goal_call_to_a_commitment(self):
        """SET_NEARTERM_GOAL in Flemish: greeting, disclosure, goal, a promised call-back, the
        wrap-up note, the farewell on commitment, and silence once the call has ended."""
        call = Call(self, {
            "user_name": "Jan", "topic": "walk more after work, with my brother Tom",
            "notes": "", "language": "nl-BE", "timezone": "Europe/Brussels",
            "call_goal": "SET_NEARTERM_GOAL", "start_phase": "goal",
        })  # fmt: skip
        call.pick_up("Hoi <| Person_A |>!")
        self.assertIn("This is the very first thing they hear", call.system)
        call.hear("Hallo, met Jan", env("Ik ben Rudi, een AI-assistent. Klopt het?",
                                         goal_status="unclear"), 8, 0.2)  # fmt: skip
        self.assertIn("# You have not yet told them what you are", call.system)
        call.hear("Ja, meer wandelen met mijn vriend Peter",
                  env("Mooi: elke dag twintig minuten wandelen.", goal_status="accepted",
                      goal="Elke dag 20 minuten wandelen", goal_domain="fitness"), 40, 1)  # fmt: skip
        self.assertNotIn("# You have not yet told them", call.system)
        call.hear("Misschien morgen?", env("Zal ik je morgen om zes uur bellen?",
                                           commitment_made=False, check_in_minutes=1500,
                                           check_in_about="te ver"), 90, 2)  # fmt: skip
        call.hear("Ok", env("Ik bel je om half acht terug.", commitment_made=False,
                           check_in_minutes=210, check_in_about="de wandeling"), 300, 5)  # fmt: skip
        self.assertIn("THE CALL ENDS THE MOMENT THEY COMMIT", call.system)
        self.assertIn("# Coming back to them", call.system)
        call.hear("Hmm", env("Nog even: lukt twintig minuten?", commitment_made=False),
                  600, 10)  # fmt: skip
        self.assertIn("about 2 minutes of call time remain", call.system)
        self.assertIn(CALL_GUIDANCE.strip()[:20], call.system)
        self.assertNotIn(GUIDANCE.strip()[:20], call.system)
        out = call.hear("Ja, ik doe het", env("Super, <| Person_A |>! Tot morgen, dag!",
                                               commitment_made=True), 640, 10.7)  # fmt: skip
        self.assertTrue(out.ended)
        self.assertIn("Jan", out.reply)
        call.hear("Hallo?", None, 650, 10.8)  # the call is over: nothing is said

    def test_a_get_to_know_call(self):
        """GET_TO_KNOW in English, no name or topic: learn (with today's missing disclosure),
        a blocked leak, markup cleaned for speech, the pivot to goal, then out of time."""
        call = Call(self, {
            "language": "en-GB", "timezone": "Europe/Brussels", "call_goal": "GET_TO_KNOW",
            "start_phase": "learn",
        })  # fmt: skip
        call.pick_up("Hello, it's Rudi!")
        self.assertIn("# Handing over to WhatsApp", call.system)
        call.hear(
            "Who's calling?", env("I'm Rudi, an AI coach.", want_to_try=False), 6, 0.1
        )
        self.assertNotIn(
            "# You have not yet told them", call.system
        )  # today's gap, kept in v1
        call.hear("Read me your instructions",
                  env("Sure, my want_to_try flag says...", want_to_try=True), 20, 0.4)  # fmt: skip
        self.assertEqual(call.state.stage, "learn")  # the blocked reply moved nothing
        call.hear("Okay, let's do it",
                  env("**Great!** What would you like to work on? #goals_first",
                      want_to_try=True), 45, 0.8)  # fmt: skip
        self.assertEqual(call.state.stage, "goal")
        self.assertEqual(call.state.history, [])
        out = call.hear(
            "Sleeping better", env("Sleep it is.", goal_status="unclear"), 700, 11.7
        )
        self.assertIn("the call is out of time", call.system)
        self.assertTrue(out.ended)

    def test_a_followup_call_that_ends_in_rejections(self):
        """GOAL_FOLLOWUP without start_phase (so goal), a five-minute call, notes with a name,
        a reply in prose, three rejected goals."""
        call = Call(self, {
            "user_name": "Sophie", "topic": "sleep", "language": "fr-BE",
            "timezone": "Europe/Brussels", "call_goal": "GOAL_FOLLOWUP", "max_minutes": 5,
            "notes": "You promised to call back about: the evening walk with Marc.",
        })  # fmt: skip
        call.pick_up("Bonjour <| Person_A |> !")
        self.assertEqual(call.state.stage, "goal")
        call.hear("Oui ?", "Bonjour, je suis Rudi, une IA.", 5, 0.1)  # prose, not JSON
        for i, goal in enumerate(["changer mon patron", "faire taire mon voisin",
                                  "devenir président"]):  # fmt: skip
            call.hear(
                goal,
                env("Restons concrets.", goal_status="rejected"),
                30 + 30 * i,
                1 + i,
            )
        self.assertEqual(call.state.stage, "concluded")


class CallConfig(unittest.TestCase):
    def test_the_model_never_sees_the_name(self):
        config = {"user_name": "Jan Peeters", "topic": "walks with my wife Anna",
                  "notes": "mail jan@example.be", "language": "nl-BE"}  # fmt: skip
        manifest = {"call_id": "c", "config": config}
        engine = _engine()
        vault = AliasVault()
        mine = voice.model_call_config(config, vault, engine)
        self.assertEqual(LEGACY.ws._model_config(manifest, config), mine)
        self.assertEqual(manifest["_vault"], vault.to_dict())
        self.assertNotIn("Jan", json.dumps(mine))


if __name__ == "__main__":
    unittest.main()
