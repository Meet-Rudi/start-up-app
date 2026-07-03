"""
AI responder state-machine tests (ported try-rudi experience) — no network, no real LLM.

boto3 is stubbed to an in-memory FakeS3 seeded with the prompt assets, and gateway.generate is
replaced with a controllable fake so we can drive the signals turn by turn.

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

# Seed the prompt/context assets the responder reads.
for key in ("prompts/rudi_guardrails.md", "prompts/rudi_learn_prompt.md",
            "prompts/rudi_goal_prompt.md", "prompts/rudi_commit_prompt.md",
            "contexts/rudi-context.md", "contexts/diabetes-t2d-guidance.md"):
    _FAKE_S3.put_object(Bucket="meetrudi-ai-data-test", Key=key, Body=("PROMPT " + key).encode())

import responder  # noqa: E402

# responder may have been imported earlier (via processor) bound to a different FakeS3;
# rebind to our seeded instance and clear its asset cache.
responder.s3 = _FAKE_S3
responder._asset_cache.clear()

# ---- controllable fake LLM ---------------------------------------------------
_NEXT: list = []


def _fake_generate(messages, json_mode=False):
    reply, signals = _NEXT.pop(0)
    return {"text": json.dumps({"reply": reply, "signals": signals}), "model": "fake"}


responder.gateway.generate = _fake_generate


def queue(reply, signals):
    _NEXT.append((reply, signals))


class ResponderTests(unittest.TestCase):
    def setUp(self):
        _NEXT.clear()

    # -- greeting (no LLM) ------------------------------------------------
    def test_new_contact_gets_intro_without_llm(self):
        reply, state, info = responder.respond({}, "hi")
        self.assertTrue(reply.startswith("👋"))
        self.assertIn("nice to meet you", reply)
        self.assertIn("what's your name", reply.lower())
        self.assertEqual(state["phase"], "learn")
        self.assertEqual(state["session_id"], 1)
        self.assertTrue(info.get("new_contact"))
        self.assertEqual(_NEXT, [])   # no model call consumed

    def test_markdown_bold_converted_to_whatsapp(self):
        self.assertEqual(responder._to_whatsapp("say **hi** now"), "say *hi* now")

    # -- learn → goal -----------------------------------------------------
    def test_learn_intent_switches_to_goal_and_clears_history(self):
        _, state, _ = responder.respond({}, "hi")               # greet
        queue("Great — what would you like to achieve?", {"want_to_try": True})
        _, state, info = responder.respond(state, "I want to try")
        self.assertEqual(state["phase"], "goal")
        self.assertEqual(state["history"], [])                  # fresh Real-Rudi session

    # -- goal accepted → commit ------------------------------------------
    def test_goal_accepted_moves_to_commit(self):
        state = {"phase": "goal", "session_id": 1, "history": [], "clarifiers_used": 0,
                 "commit_attempts": 0, "reject_count": 0, "goal": None, "goal_domain": None}
        queue("Love it — walking daily.", {"goal_status": "accepted", "goal": "walk daily",
                                            "goal_domain": "fitness"})
        _, state, _ = responder.respond(state, "I want to walk every day")
        self.assertEqual(state["phase"], "commit")
        self.assertEqual(state["goal"], "walk daily")
        self.assertEqual(state["goal_domain"], "fitness")

    def test_goal_unclear_then_force_accept_after_budget(self):
        state = {"phase": "goal", "session_id": 1, "history": [], "clarifiers_used": 0,
                 "commit_attempts": 0, "reject_count": 0, "goal": None, "goal_domain": None}
        queue("Can you say more?", {"goal_status": "unclear"})
        _, state, _ = responder.respond(state, "get better")
        self.assertEqual(state["phase"], "goal")
        self.assertEqual(state["clarifiers_used"], 1)
        queue("Still a bit vague?", {"goal_status": "unclear"})
        _, state, _ = responder.respond(state, "you know, better")
        self.assertEqual(state["clarifiers_used"], 2)
        # third unclear: clarifier budget exhausted → force-accept into commit
        queue("Hmm.", {"goal_status": "unclear"})
        _, state, _ = responder.respond(state, "just better")
        self.assertEqual(state["phase"], "commit")

    def test_goal_rejected_three_times_concludes(self):
        state = {"phase": "goal", "session_id": 1, "history": [], "clarifiers_used": 0,
                 "commit_attempts": 0, "reject_count": 0, "goal": None, "goal_domain": None}
        for i in range(2):
            queue("Let's keep it about you.", {"goal_status": "rejected"})
            _, state, _ = responder.respond(state, "change the government")
            self.assertEqual(state["phase"], "goal")
        queue("Let's keep it real.", {"goal_status": "rejected"})
        _, state, _ = responder.respond(state, "fix everyone else")
        self.assertEqual(state["phase"], "concluded")

    # -- commit -----------------------------------------------------------
    def test_commitment_made_concludes(self):
        state = {"phase": "commit", "session_id": 1, "history": [], "clarifiers_used": 0,
                 "commit_attempts": 0, "reject_count": 0, "goal": "walk daily", "goal_domain": "fitness"}
        queue("Amazing — 10 min after lunch. I'll check in!", {"commitment_made": True})
        _, state, _ = responder.respond(state, "ok I'll walk 10 min after lunch")
        self.assertEqual(state["phase"], "concluded")

    def test_commit_exhausts_attempts_and_concludes(self):
        state = {"phase": "commit", "session_id": 1, "history": [], "clarifiers_used": 0,
                 "commit_attempts": 0, "reject_count": 0, "goal": "walk daily", "goal_domain": "fitness"}
        for i in range(6):
            queue("How about a small step?", {"commitment_made": False})
            _, state, _ = responder.respond(state, "not sure")
            self.assertEqual(state["phase"], "commit")
        queue("I'll let you reflect and check in later.", {"commitment_made": False})
        _, state, _ = responder.respond(state, "maybe later")
        self.assertEqual(state["phase"], "concluded")

    # -- concluded restarts ----------------------------------------------
    def test_concluded_restarts_with_welcome_back(self):
        state = {"phase": "concluded", "session_id": 3, "history": [{"role": "assistant", "content": "bye"}]}
        reply, state, info = responder.respond(state, "hey again")
        self.assertTrue(reply.startswith("👋"))
        self.assertFalse(info.get("new_contact"))   # returning, not a new contact
        self.assertEqual(state["phase"], "learn")
        self.assertEqual(state["session_id"], 4)    # session counter advanced
        self.assertEqual(_NEXT, [])                 # greeting, no model call

    # -- language scaffold ------------------------------------------------
    def test_language_signal_captured(self):
        state = {"phase": "commit", "session_id": 1, "history": [], "clarifiers_used": 0,
                 "commit_attempts": 0, "reject_count": 0, "goal": "walk", "goal_domain": "fitness"}
        queue("Super — 10 Minuten nach dem Mittagessen?", {"commitment_made": False, "lang": "de"})
        _, state, info = responder.respond(state, "ich bin nicht sicher", locale="de")
        self.assertEqual(info["lang"], "de")

    def test_i18n_fallback_to_english(self):
        import i18n
        self.assertEqual(i18n.t("tired", "xx"), i18n.STRINGS["en"]["tired"])       # unknown locale → en
        self.assertEqual(i18n.normalize_locale("de-DE"), "de")
        self.assertIsNone(i18n.normalize_locale(""))

    def test_translations_present_for_de_fr_nl(self):
        import i18n
        for loc in ("de", "fr", "nl"):
            for key in ("intro", "welcome_back", "media_ack", "tired", "error"):
                s = i18n.t(key, loc)
                self.assertTrue(s and s != i18n.STRINGS["en"][key],   # actually translated, not the en string
                                "%s/%s missing or identical to English" % (loc, key))
        # media_ack tolerates the kind kwarg even when the translation omits {kind}
        self.assertTrue(i18n.t("media_ack", "de", kind="image"))

    def test_welcome_back_uses_new_text(self):
        state = {"phase": "concluded", "session_id": 1, "history": []}
        reply, _, _ = responder.respond(state, "hi", locale="en")
        self.assertIn("How are things rolling", reply)

    # -- health guidance injected in commit ------------------------------
    def test_commit_system_prompt_includes_health_guidance_for_health_domain(self):
        sys_txt = responder._build_system("commit", {"goal": "walk", "goal_domain": "diabetes", "attempts_left": 7})
        self.assertIn("diabetes-t2d-guidance", sys_txt)   # seeded body marker
        self.assertIn("PROMPT prompts/rudi_guardrails.md", sys_txt)


if __name__ == "__main__":
    unittest.main()
