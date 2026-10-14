"""
meetrudi-engine-turn tests — the Training Arena's door into the deployed Rudi engine.

What is asserted is what would hurt if it regressed:
  - an arena turn can never spend live Rudi's keys: a live secret, an unknown host, an empty or
    all-disabled cascade is refused, and inside gateway.using() the in-code Groq fallback (which
    carries the live key) is never tried
  - a turn through engine_turn gives the same reply as the tester console's chat for the same
    state and input — both run webchat.chat_turn
  - identifiers are scrubbed before the engine sees the text, and the scan op finds them

No boto3, no network, synthetic data only.
Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

if "boto3" not in sys.modules:
    _boto3 = types.ModuleType("boto3")
    _boto3.client = lambda name, *a, **k: object()
    sys.modules["boto3"] = _boto3
os.environ.setdefault("DATA_BUCKET", "meetrudi-ai-data-test")

import deid  # noqa: E402
import gateway  # noqa: E402
import webchat  # noqa: E402
import engine_turn  # noqa: E402

LIVE_HOSTS = {"api.groq.com", "oai.endpoints.kepler.ai.cloud.ovh.net"}
ARENA = [
    {"name": "groq-gpt-oss-120b", "kind": "groq", "endpoint": "https://api.groq.com/openai/v1/chat/completions",
     "model": "openai/gpt-oss-120b", "secret": "meetrudi/arena/rudi-groq"},
    {"name": "ovh-gpt-oss-120b", "kind": "openai_compatible",
     "endpoint": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/chat/completions",
     "model": "gpt-oss-120b", "secret": "meetrudi/arena/rudi-ovh"},
]

# Captured now: test_responder swaps gateway.generate for a fake at import time.
_REAL_GENERATE = gateway.generate


class _EchoEngine:
    """Deterministic stand-in for responder: echoes the (already de-identified) text."""
    calls: list = []

    @staticmethod
    def asset_versions():
        return {"prompts/rudi_guardrails.md": "v-test"}

    @staticmethod
    def respond(state, user_text, locale="en", personality_block="", tz="", now=None):
        _EchoEngine.calls.append(user_text)
        if not state:
            return ("Hi, I'm Rudi.", {"phase": "learn"}, {"phase": "learn"})
        return ("Heard you: " + user_text, dict(state, turns=state.get("turns", 0) + 1),
                {"phase": "goal", "phase_before": state.get("phase"), "model": "groq-gpt-oss-120b",
                 "lang": locale, "signals": {"want_to_try": True}, "leak_blocked": False,
                 "usage": {"prompt_tokens": 3100, "completion_tokens": 240}})


class _NoPersona:
    @staticmethod
    def resolve_block(slug):
        return ""


class CascadeGuardTests(unittest.TestCase):
    def test_accepts_arena_keys_on_live_providers(self):
        out = engine_turn.check_cascade(ARENA, allowed=LIVE_HOSTS)
        self.assertEqual([e["secret"] for e in out], ["meetrudi/arena/rudi-groq", "meetrudi/arena/rudi-ovh"])

    def test_refuses_a_live_key(self):
        bad = [dict(ARENA[0], secret="meetrudi-groq-firstkey")]
        with self.assertRaises(engine_turn.BadCascade):
            engine_turn.check_cascade(bad, allowed=LIVE_HOSTS)

    def test_refuses_the_arena_simulation_key(self):
        # The tester / judge key is the arena's, but it is not Rudi's: it must not answer as Rudi.
        bad = [dict(ARENA[1], secret="meetrudi/arena/sim-ovh")]
        with self.assertRaises(engine_turn.BadCascade):
            engine_turn.check_cascade(bad, allowed=LIVE_HOSTS)

    def test_refuses_a_provider_live_rudi_does_not_use(self):
        bad = [dict(ARENA[1], endpoint="https://api.example.com/v1/chat/completions")]
        with self.assertRaises(engine_turn.BadCascade):
            engine_turn.check_cascade(bad, allowed=LIVE_HOSTS)

    def test_refuses_plain_http_empty_and_all_disabled(self):
        for bad in ([dict(ARENA[1], endpoint="http://oai.endpoints.kepler.ai.cloud.ovh.net/x")], [], None,
                    [dict(e, enabled=False) for e in ARENA]):
            with self.assertRaises(engine_turn.BadCascade):
                engine_turn.check_cascade(bad, allowed=LIVE_HOSTS)


class NoLiveFallbackTests(unittest.TestCase):
    def setUp(self):
        self._call, self._sleep, self._load = gateway._registry.call, gateway.time.sleep, gateway._load_endpoints
        gateway.time.sleep = lambda s: None
        gateway._load_endpoints = lambda: [{"name": "live", "secret": "meetrudi-groq-firstkey"}]

    def tearDown(self):
        gateway._registry.call, gateway.time.sleep, gateway._load_endpoints = self._call, self._sleep, self._load

    def test_exhausted_arena_cascade_never_reaches_the_live_key(self):
        used = []

        def call(cfg, messages, timeout=25, json_mode=False):
            used.append(cfg["secret"])
            raise gateway.RateLimitError("429")

        gateway._registry.call = call
        with gateway.using(ARENA):
            with self.assertRaises(gateway.AllRateLimited):
                _REAL_GENERATE([{"role": "user", "content": "hi"}])
        self.assertTrue(used)
        self.assertTrue(all(s.startswith("meetrudi/arena/rudi-") for s in used), used)

    def test_outside_the_block_the_live_cascade_is_back(self):
        used = []

        def call(cfg, messages, timeout=25, json_mode=False):
            used.append(cfg["secret"])
            return "ok"

        gateway._registry.call = call
        with gateway.using(ARENA):
            pass
        self.assertEqual(_REAL_GENERATE([{"role": "user", "content": "hi"}])["text"], "ok")
        self.assertEqual(used, ["meetrudi-groq-firstkey"])

    def test_usage_is_reported(self):
        def call(cfg, messages, timeout=25, json_mode=False):
            gateway._registry.last_usage = {"prompt_tokens": 10, "completion_tokens": 2}
            return "ok"

        gateway._registry.call = call
        with gateway.using(ARENA):
            r = _REAL_GENERATE([{"role": "user", "content": "hi"}])
        self.assertEqual(r["usage"], {"prompt_tokens": 10, "completion_tokens": 2})
        self.assertEqual(r["model"], "groq-gpt-oss-120b")


class TurnTests(unittest.TestCase):
    def setUp(self):
        self._chat, self._hosts = webchat.chat_turn, engine_turn._allowed_hosts
        self._responder, self._personality = webchat.responder, webchat.personality
        webchat.responder, webchat.personality = _EchoEngine, _NoPersona
        engine_turn._allowed_hosts = lambda: set(LIVE_HOSTS)
        _EchoEngine.calls.clear()

    def tearDown(self):
        webchat.chat_turn, engine_turn._allowed_hosts = self._chat, self._hosts
        webchat.responder, webchat.personality = self._responder, self._personality

    def _turn(self, text, state=None, **kw):
        ev = {"op": "turn", "channel": "chat", "state": state or {"phase": "learn"}, "vault": None,
              "text": text, "locale": "en", "cascade": ARENA, "trace": {"case": "TA-20261014-AB12-0001", "turn": 1}}
        ev.update(kw)
        return engine_turn.handler(ev)

    def test_turn_returns_reply_state_usage_and_engine(self):
        out = self._turn("I want to walk more")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["reply"], "Heard you: I want to walk more")
        self.assertEqual(out["info"]["phase"], "goal")
        self.assertEqual(out["usage"], {"model": "groq-gpt-oss-120b", "tokens_in": 3100, "tokens_out": 240, "estimated": False})
        self.assertIn("release", out["engine"])

    def test_the_cascade_is_in_force_during_the_turn(self):
        seen = []

        def spy(*a, **k):
            seen.append(gateway._override.get())
            return self._chat(*a, **k)

        webchat.chat_turn = spy
        self.assertTrue(self._turn("hello")["ok"])
        self.assertEqual([e["secret"] for e in seen[0]], ["meetrudi/arena/rudi-groq", "meetrudi/arena/rudi-ovh"])
        self.assertIsNone(gateway._override.get())          # and gone again afterwards

    def test_bad_cascade_is_an_error_not_a_fallback(self):
        out = self._turn("hello", cascade=[dict(ARENA[0], secret="meetrudi-groq-firstkey")])
        self.assertEqual(out["error"], "bad_cascade")
        self.assertEqual(_EchoEngine.calls, [])

    def test_identifiers_never_reach_the_engine(self):
        out = self._turn("call me on +32 470 12 34 56 please")
        self.assertTrue(out["ok"])
        self.assertNotIn("470", _EchoEngine.calls[-1])
        self.assertEqual(out["info"]["pii_redacted"].get("phone-number"), 1)

    def test_same_reply_as_the_tester_console_chat(self):
        state = {"phase": "goal", "turns": 2}
        via_engine = self._turn("Walk after dinner", state=state)["reply"]
        direct = webchat.chat_turn(deid.AliasVault(), state, "Walk after dinner", "en", "",
                                   engine=_EchoEngine, personas=_NoPersona)["reply"]
        self.assertEqual(via_engine, direct)

    def test_rate_limited_and_failed_engine_are_reported(self):
        def limited(*a, **k):
            raise gateway.AllRateLimited("all 429")

        def broken(*a, **k):
            raise gateway.AIError("down")

        webchat.chat_turn = limited
        self.assertEqual(self._turn("hi")["error"], "rate_limited")
        webchat.chat_turn = broken
        self.assertEqual(self._turn("hi")["error"], "engine_failed")

    def test_open_is_the_canned_greeting_without_a_cascade(self):
        orig = engine_turn.responder
        engine_turn.responder = _EchoEngine
        try:
            out = engine_turn.handler({"op": "open", "locale": "en"})
        finally:
            engine_turn.responder = orig
        self.assertTrue(out["ok"])
        self.assertEqual(out["reply"], "Hi, I'm Rudi.")
        self.assertEqual(out["usage"]["tokens_in"], 0)

    def test_scan_finds_identifiers(self):
        out = engine_turn.handler({"op": "scan", "texts": ["mail tom@example.org", "just walking"]})
        self.assertTrue(out["ok"])
        self.assertEqual(out["found"][0].get("email-address"), 1)
        self.assertEqual(out["found"][1], {})

    def test_info_lists_personalities(self):
        orig = engine_turn.personality
        engine_turn.personality = types.SimpleNamespace(
            DEFAULT_SLUG="seed-rudi-v2", list_available=lambda: [{"slug": "seed-rudi-v2"}, {"slug": "seed-rudi-v3"}])
        try:
            out = engine_turn.handler({"op": "info"})
        finally:
            engine_turn.personality = orig
        self.assertEqual(out["personalities"], ["seed-rudi-v2", "seed-rudi-v3"])
        self.assertEqual(out["default_personality"], "seed-rudi-v2")

    def test_unknown_op_and_other_channels_are_refused(self):
        self.assertEqual(engine_turn.handler({"op": "nope"})["error"], "bad_request")
        self.assertEqual(engine_turn.handler({"op": "turn", "channel": "call"})["error"], "bad_request")


if __name__ == "__main__":
    unittest.main()
