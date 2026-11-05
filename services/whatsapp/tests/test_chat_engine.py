"""
chat_engine — which engine answers a WhatsApp or tester-chat message (decision #40).

What is asserted is what would hurt if it regressed:
  - CHAT_ENGINE picks the engine; anything unknown means legacy
  - the legacy path is today's call, unchanged (persona block, clock, the caller's stubs)
  - the platform path runs CM's clone on the platform Turn Engine and answers in the legacy
    contract: a de-identified reply, CM's flat state, the info the processor reads
  - a platform that cannot load or breaks mid-turn costs nothing: that message runs on legacy
  - model failures are not "breaks": they surface as the legacy gateway's own errors
  - the live cascade is the S3 config plus the in-code fallback; a given cascade (the arena's)
    replaces all of it, so an arena turn never reaches a live key
  - through the processor: the person reads their name, S3 keeps only the alias, the engine is
    logged; a platform-gate halt freezes the contact
  - the arena door runs either engine and says which one answered

No boto3, no network, synthetic data only.
Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)
# The platform Turn Engine, as the meetrudi-tenant-prod-pool-engine-whatsapp layer ships it.
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "packages"))

from fake_s3 import FakeS3

BUCKET = "meetrudi-ai-data-test"
os.environ.setdefault("DATA_BUCKET", BUCKET)
os.environ.setdefault("OBJECTION_CLASSIFIER", "false")
if "boto3" not in sys.modules:          # alone; under discover another module stubbed it already
    _boto3 = types.ModuleType("boto3")
    _boto3.client = lambda name, *a, **k: FakeS3()
    sys.modules["boto3"] = _boto3

import rudi_skills
from rudi_platform.engine import DictAssetStore, load_clone
from rudi_platform.gateway import ModelGateway, RateLimitError, StaticSecrets
from rudi_platform.registry import PLATFORM, InMemoryRegistryStore
from rudi_tenants import cm

NOW = datetime.datetime(2026, 9, 14, 7, 30, tzinfo=datetime.UTC)
ASSETS = {cm.ASSETS["about"]: "About Rudi.", cm.ASSETS["health_guidance"]: "Walk after meals."}
LIVE_KEY = "meetrudi-groq-firstkey"
ARENA = [
    {"name": "groq-gpt-oss-120b", "kind": "groq",
     "endpoint": "https://api.groq.com/openai/v1/chat/completions",
     "model": "openai/gpt-oss-120b", "secret": "meetrudi/arena/rudi-groq"},
]

# Imported in setUpClass, not here: `unittest discover` imports every test module first, and the
# modules below bind their S3 clients at import. Importing them here would make this file decide
# those clients for the whole run (see test_deid_wiring.py).
chat_engine = gateway = None


class Model:
    """The provider behind the platform gateway: scripted replies, every payload kept."""

    def __init__(self):
        self.replies, self.payloads, self.urls = [], [], []

    def post_json(self, url, headers, payload, timeout):
        self.urls.append(url)
        self.payloads.append(payload)
        reply = self.replies.pop(0) if self.replies else _env("Alright.")
        if isinstance(reply, Exception):
            raise reply
        return {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3}}


def _env(reply, **signals):
    return json.dumps({"reply": reply, "signals": signals})


class Legacy:
    """responder-shaped stand-in that records how it was called."""

    def __init__(self):
        self.calls = []

    def respond(self, state, text, locale="en", personality_block="", **clock):
        self.calls.append({"state": state, "text": text, "locale": locale,
                           "block": personality_block, **clock})
        return "legacy reply", dict(state or {}, phase="goal"), {"phase": "goal", "lang": locale}


class Personas:
    @staticmethod
    def resolve_block(slug):
        return f"BLOCK({slug})"


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global chat_engine, gateway
        import chat_engine as _ce
        import gateway as _gw
        chat_engine, gateway = _ce, _gw
        store = InMemoryRegistryStore()
        seeded = cm.seed(store, rudi_skills.manifests())
        cls.clone = load_clone(store, PLATFORM, seeded.version_id, rudi_skills.runtime(),
                               DictAssetStore(ASSETS))

    def setUp(self):
        self.model = Model()
        models = ModelGateway(self.model, StaticSecrets({LIVE_KEY: "k",
                                                         "meetrudi/arena/rudi-groq": "a"}),
                              sleep=lambda s: None)
        self._saved = (chat_engine._loaded, chat_engine._s3, os.environ.get("CHAT_ENGINE"))
        chat_engine._loaded = (self.clone, models)
        chat_engine._s3 = FakeS3()             # no endpoints config: the fallback only
        os.environ.pop("CHAT_ENGINE", None)
        self.legacy = Legacy()

    def tearDown(self):
        chat_engine._loaded, chat_engine._s3, engine = self._saved
        if engine is None:
            os.environ.pop("CHAT_ENGINE", None)
        else:
            os.environ["CHAT_ENGINE"] = engine

    def respond(self, state, text, **kw):
        kw.setdefault("locale", "en")
        kw.setdefault("now", NOW)
        kw.setdefault("legacy", self.legacy)
        kw.setdefault("personas", Personas)
        return chat_engine.respond(state, text, **kw)


class Choice(Case):
    def test_chat_engine_picks_the_engine_and_unknown_means_legacy(self):
        self.assertEqual(chat_engine.default_engine(), "legacy")
        for value, want in (("platform", "platform"), (" Platform ", "platform"),
                            ("legacy", "legacy"), ("brain", "legacy"), ("", "legacy")):
            os.environ["CHAT_ENGINE"] = value
            self.assertEqual(chat_engine.default_engine(), want, value)

    def test_legacy_is_todays_call_unchanged(self):
        reply, _state, info = self.respond({"phase": "learn"}, "hi", persona="seed-rudi-v3",
                                          tz="Europe/Brussels")
        self.assertEqual(reply, "legacy reply")
        self.assertEqual(info["engine"], "legacy")
        call = self.legacy.calls[0]
        self.assertEqual((call["text"], call["block"], call["tz"], call["now"]),
                         ("hi", "BLOCK(seed-rudi-v3)", "Europe/Brussels", NOW))
        self.assertEqual(self.model.payloads, [])

    def test_a_given_persona_block_is_used_as_is_and_no_tz_is_not_sent(self):
        self.respond({"phase": "learn"}, "hi", personality_block="")
        self.assertEqual(self.legacy.calls[0]["block"], "")
        self.assertNotIn("tz", self.legacy.calls[0])


class Platform(Case):
    def test_a_new_contact_is_greeted_without_a_model_call(self):
        reply, state, info = self.respond({}, "hello", engine="platform")
        self.assertIn("Rudi", reply)
        self.assertEqual((state["phase"], info["engine"], info["phase"]),
                         ("learn", "platform", "learn"))
        self.assertTrue(info["greeted"] and info["new_contact"])
        self.assertEqual(self.model.payloads, [])
        self.assertEqual(self.legacy.calls, [])

    def test_a_turn_answers_in_the_legacy_contract(self):
        import deid

        _, state, _ = self.respond({}, "hello", engine="platform")
        vault = deid.AliasVault()
        masked, _ = deid.scrub_inbound(vault, "I walk with Peter on Sundays", deid.HeuristicDetector(),
                                       "en")
        self.model.replies.append(_env("Nice, <| Person_A |> sounds like good company!",
                                       want_to_try=False, lang="en"))
        reply, state, info = self.respond(state, masked, engine="platform", vault=vault)
        self.assertEqual(reply, "Nice, <| Person_A |> sounds like good company!")
        self.assertEqual(deid.restore_outbound(vault, reply), "Nice, Peter sounds like good company!")
        self.assertEqual(info["engine"], "platform")
        self.assertEqual(info["model"], "groq-fallback")
        self.assertEqual((info["phase_before"], info["phase"], info["lang"]),
                         ("learn", "learn", "en"))
        self.assertEqual(info["usage"], {"prompt_tokens": 12, "completion_tokens": 3})
        self.assertEqual(state["phase"], "learn")          # CM's flat state, as responder keeps it
        self.assertEqual(state["history"][-1]["content"], reply)
        sent = json.dumps(self.model.payloads[0]["messages"])
        self.assertNotIn("Peter", sent)                    # the model saw the alias only
        self.assertIn("Person_A", sent)

    def test_the_live_cascade_is_the_config_then_the_fallback(self):
        s3 = FakeS3()
        s3.put_object(Bucket=BUCKET, Key="config/ai_endpoints.json", Body=json.dumps(
            {"endpoints": [
                {"name": "groq-gpt-oss-120b", "kind": "groq", "model": "openai/gpt-oss-120b",
                 "endpoint": "https://api.groq.com/openai/v1/chat/completions",
                 "secret": LIVE_KEY},
                {"name": "off", "kind": "groq", "model": "m", "enabled": False,
                 "endpoint": "https://api.groq.com/openai/v1/chat/completions",
                 "secret": LIVE_KEY}]}).encode("utf-8"))
        chat_engine._s3 = s3
        self.assertEqual([e.name for e in chat_engine._cascade()],
                         ["groq-gpt-oss-120b", "groq-fallback"])
        self.assertEqual([e.name for e in chat_engine._cascade(ARENA)], ["groq-gpt-oss-120b"])
        self.assertEqual(chat_engine._cascade([]), [])

    def test_a_given_cascade_never_reaches_the_live_key(self):
        self.model.replies.extend([RateLimitError("429")] * 3)
        with self.assertRaises(gateway.AllRateLimited):
            self.respond({"phase": "learn", "history": []}, "hi", engine="platform",
                         cascade=ARENA)
        self.assertEqual(len(self.model.payloads), 3)      # one endpoint, its two retries
        self.assertTrue(all("groq" in u for u in self.model.urls))
        self.assertEqual(self.legacy.calls, [])            # a model failure is not a fallback

    def test_model_failures_are_the_legacy_gateways_errors(self):
        self.model.replies.extend([RateLimitError("429")] * 3)
        with self.assertRaises(gateway.AllRateLimited):
            self.respond({"phase": "learn", "history": []}, "hi", engine="platform")
        self.model.replies.append(RuntimeError("socket closed"))
        with self.assertRaises(gateway.AIError):
            self.respond({"phase": "learn", "history": []}, "hi", engine="platform")
        self.assertEqual(self.legacy.calls, [])

    def test_a_platform_that_cannot_load_answers_on_legacy(self):
        chat_engine._loaded = None
        real = chat_engine._load

        def broken():
            raise ImportError("no layer")

        chat_engine._load = broken
        try:
            reply, _, info = self.respond({"phase": "learn"}, "hi", engine="platform")
        finally:
            chat_engine._load = real
        self.assertEqual((reply, info["engine"]), ("legacy reply", "legacy"))

    def test_a_platform_that_breaks_mid_turn_answers_on_legacy(self):
        import rudi_platform.engine as platform_engine

        real = platform_engine.TurnEngine.turn

        def broken(*a, **k):
            raise RuntimeError("a skill broke")

        platform_engine.TurnEngine.turn = broken
        try:
            reply, _, info = self.respond({"phase": "learn", "history": []}, "hi",
                                          engine="platform")
        finally:
            platform_engine.TurnEngine.turn = real
        self.assertEqual((reply, info["engine"]), ("legacy reply", "legacy"))

    def test_an_inbound_gate_halt_is_reported(self):
        _reply, state, info = self.respond({"phase": "goal", "history": []},
                                          "Wrong number, who is this?", engine="platform")
        self.assertEqual(info["halted"]["kind"], "wrong_number")
        self.assertEqual(state["phase"], "goal")
        self.assertEqual(self.model.payloads, [])

    def test_a_blocked_reply_says_why(self):
        self.model.replies.append(json.dumps({"response": "x", "goal_status": "unclear"}))
        _, _, info = self.respond({"phase": "goal", "history": []}, "hi", engine="platform")
        self.assertTrue(info["leak_blocked"])
        self.assertEqual(info["guard"], {"leak": "marker",
                                         "envelope": "json-no-reply:goal_status,response"})

    def test_the_responder_shaped_object_routes_and_remembers(self):
        engine = chat_engine.Responder("seed-rudi-v2", None, legacy=self.legacy,
                                       personas=Personas, engine="platform")
        engine.respond({}, "hi", locale="nl", personality_block="ignored")
        self.assertEqual(engine.answered, "platform")
        engine = chat_engine.Responder("seed-rudi-v2", None, legacy=self.legacy,
                                       personas=Personas, engine="legacy")
        engine.respond({"phase": "learn"}, "hi", locale="nl", personality_block="given")
        self.assertEqual((engine.answered, self.legacy.calls[-1]["block"]), ("legacy", "given"))


class Processor(Case):
    """One inbound WhatsApp message through processor.handler on the platform engine."""

    def setUp(self):
        super().setUp()
        import processor
        import provider
        import store

        self.processor, self.store = processor, store
        self.sent = []
        self._real = (processor.STORE._s3, getattr(store, "s3", None), processor.AI_RESPONDER,
                      provider.send_text, processor.provider.send_text)
        self.s3 = FakeS3()
        processor.STORE._s3 = self.s3
        store.s3 = self.s3
        processor.AI_RESPONDER = True
        processor.provider.send_text = lambda to, body: self.sent.append(body)
        os.environ["CHAT_ENGINE"] = "platform"

    def tearDown(self):
        p = self.processor
        p.STORE._s3, s3, p.AI_RESPONDER, _send, p.provider.send_text = self._real
        if s3 is not None:
            self.store.s3 = s3
        super().tearDown()

    def _inbound(self, text, phone="+32470000001"):
        self.processor.handler({"Records": [{"body": json.dumps(
            {"user_phone": phone, "text": text, "type": "text",
             "provider_msg_id": f"SM{len(self.sent)}"})}]}, None)
        return self.store.user_id(phone, self.processor.SALT)

    def test_the_person_reads_their_name_and_s3_keeps_the_alias(self):
        self._inbound("hello")                                   # the greeting, no model call
        self.model.replies.append(_env("Say hi to <| Person_A |> from me!",
                                       want_to_try=False, lang="en"))
        uid = self._inbound("My friend Peter walks with me")
        self.assertEqual(self.sent[-1], "Say hi to Peter from me!")
        # Messages and the conversation state keep the alias only; the name lives solely in the
        # session's alias map on the contact record, which expires with the 24h window.
        messages = json.dumps({k: v.decode("utf-8", "replace")
                               for k, v in self.s3._store.get(BUCKET, {}).items()
                               if uid in k and "/messages/" in k})
        self.assertIn("Person_A", messages)
        self.assertNotIn("Peter", messages)
        meta = self.processor.STORE.get_meta(uid)
        self.assertNotIn("Peter", json.dumps(meta.ai_state))
        self.assertEqual(meta.ai_state["history"][-1]["content"],
                         "Say hi to <| Person_A |> from me!")
        self.assertEqual(meta.alias_vault["mapping"], {"Peter": "Person_A"})
        self.assertNotIn("Peter", json.dumps(self.model.payloads[-1]["messages"]))

    def test_a_platform_gate_halt_freezes_the_contact(self):
        self._inbound("hello")
        real = self.processor.chat_engine.respond

        def halted(*a, **k):
            return "", {}, {"halted": {"kind": "unsubscribe", "evidence": "x", "source": "lexicon"},
                            "engine": "platform"}

        self.processor.chat_engine.respond = halted
        try:
            uid = self._inbound("ok")
        finally:
            self.processor.chat_engine.respond = real
        meta = self.processor.STORE.get_meta(uid)
        self.assertEqual((meta.status, meta.frozen_kind), ("frozen", "unsubscribe"))


class ArenaDoor(Case):
    def setUp(self):
        super().setUp()
        import engine_turn

        self.door = engine_turn
        self._hosts = engine_turn._allowed_hosts
        engine_turn._allowed_hosts = lambda: {"api.groq.com"}

    def tearDown(self):
        self.door._allowed_hosts = self._hosts
        super().tearDown()

    def _turn(self, **kw):
        ev = {"op": "turn", "state": {"phase": "learn", "history": []}, "text": "I walk daily",
              "locale": "en", "cascade": ARENA, **kw}
        return self.door.handler(ev)

    def test_engine_platform_runs_on_the_arena_keys_and_says_so(self):
        self.model.replies.append(_env("Lovely.", want_to_try=False, lang="en"))
        out = self._turn(engine="platform")
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["reply"], out["engine"]["name"]), ("Lovely.", "platform"))
        self.assertEqual(out["usage"]["model"], "groq-gpt-oss-120b")
        self.assertTrue(all("groq" in u for u in self.model.urls))

    def test_without_engine_the_door_follows_chat_engine(self):
        os.environ["CHAT_ENGINE"] = "platform"
        self.model.replies.append(_env("Lovely.", want_to_try=False, lang="en"))
        self.assertEqual(self._turn()["engine"]["name"], "platform")

    def test_open_on_the_platform_is_the_canned_greeting(self):
        out = self.door.handler({"op": "open", "locale": "en", "engine": "platform"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["engine"]["name"], "platform")
        self.assertIn("Rudi", out["reply"])
        self.assertEqual(self.model.payloads, [])

    def test_an_unknown_engine_is_a_bad_request(self):
        self.assertEqual(self._turn(engine="brain")["error"], "bad_request")


if __name__ == "__main__":
    unittest.main()
