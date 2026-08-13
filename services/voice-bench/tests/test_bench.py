"""
Voice bench tests — no network, no real LLM, no real speech provider.

boto3 is stubbed to an in-memory FakeS3 seeded with the prompt assets; gateway.generate and the
Groq speech calls are replaced with controllable fakes so a whole call can be driven turn by
turn. Synthetic data only (CLAUDE.md §8).

Run:  python -m unittest discover -s services/voice-bench/tests -v
"""

from __future__ import annotations

import os
import sys
import json
import types
import base64
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from fake_s3 import FakeS3  # noqa: E402

BUCKET = "meetrudi-ai-data-test"
os.environ.setdefault("DATA_BUCKET", BUCKET)

_FAKE_S3 = FakeS3()
boto3_stub = types.ModuleType("boto3")
boto3_stub.client = lambda name, *a, **k: _FAKE_S3
sys.modules["boto3"] = boto3_stub

# Seed the prompt/context assets the brain reads.
for _key, _text in {
    "prompts/rudi_guardrails.md": "# Guardrails\nNever give medical advice.",
    "prompts/rudi_learn_prompt.md": "# Learn\nIntroduce yourself.",
    "prompts/rudi_goal_prompt.md": "# Goal\nElicit one goal.",
    "prompts/rudi_commit_prompt.md": "# Commit\nSecure one commitment.",
    "contexts/rudi-context.md": "# About Rudi\nA lifestyle buddy.",
    "contexts/health-coaching-guidance.md": "# T2D guidance\nLifestyle only.",
}.items():
    _FAKE_S3.put_object(Bucket=BUCKET, Key=_key, Body=_text.encode("utf-8"))

import brain      # noqa: E402
import calllog    # noqa: E402
import speech     # noqa: E402
import app        # noqa: E402

# ------------------------------------------------------------------ controllable fakes

_REPLIES: list = []
_LAST_SYSTEM: dict = {}


def _fake_generate(messages, json_mode=False):
    _LAST_SYSTEM["text"] = messages[0]["content"]
    if not _REPLIES:
        return {"text": json.dumps({"reply": "Alright.", "signals": {}}), "model": "fake"}
    reply, signals = _REPLIES.pop(0)
    if json_mode:
        return {"text": json.dumps({"reply": reply, "signals": signals}), "model": "fake"}
    return {"text": reply, "model": "fake"}


brain.gateway.generate = _fake_generate
app.gateway.generate = _fake_generate

speech.transcribe = lambda audio, mime="audio/webm", language="en", hint="": ("i want to walk more", 120)


def _fake_synthesize(text, voice=None, fmt=None, model=None, sentence_gap_ms=None,
                     length_scale=None):
    _SYNTH_CALLS.append({"text": text, "voice": voice, "sentence_gap_ms": sentence_gap_ms,
                         "length_scale": length_scale})
    return b"RIFFfake", "audio/wav", 200


_SYNTH_CALLS: list = []
speech.synthesize = _fake_synthesize
app.speech.transcribe = speech.transcribe
app.speech.synthesize = speech.synthesize


def _config(**over):
    cfg = {"language": "en", "user_name": "Test", "topic": "walking daily",
           "voice": "en_US-ryan-medium", "start_phase": "goal", "max_minutes": 12,
           "store_audio": True, "sentence_gap_ms": 300, "notes": ""}
    cfg.update(over)
    return cfg


def _post(payload):
    ev = {"body": json.dumps(payload), "requestContext": {"http": {"sourceIp": "127.0.0.1"}},
          "headers": {"user-agent": "unittest"}}
    return json.loads(app.handler(ev, None)["body"])


# ------------------------------------------------------------------ tests

class PhaseMachine(unittest.TestCase):
    """The ported learn/goal/commit machine must behave identically to the WhatsApp responder."""

    def test_accepted_goal_moves_to_commit(self):
        state = brain.new_state(_config())
        brain.advance(state, {"goal_status": "accepted", "goal": "walk 20 min",
                              "goal_domain": "fitness"}, "walk more", 2)
        self.assertEqual(state["phase"], "commit")
        self.assertEqual(state["goal"], "walk 20 min")
        self.assertEqual(state["commit_attempts"], 0)

    def test_unclear_burns_a_clarifier(self):
        state = brain.new_state(_config())
        brain.advance(state, {"goal_status": "unclear"}, "dunno", 2)
        self.assertEqual(state["phase"], "goal")
        self.assertEqual(state["clarifiers_used"], 1)

    def test_clarifier_budget_forces_acceptance(self):
        state = brain.new_state(_config())
        brain.advance(state, {"goal_status": "unclear"}, "maybe walking", 0)
        self.assertEqual(state["phase"], "commit")
        self.assertEqual(state["goal"], "maybe walking")

    def test_repeated_rejects_conclude(self):
        state = brain.new_state(_config())
        for _ in range(brain.MAX_REJECTS):
            brain.advance(state, {"goal_status": "rejected"}, "something illegal", 2)
        self.assertEqual(state["phase"], "concluded")

    def test_commitment_concludes(self):
        state = brain.new_state(_config())
        state["phase"] = "commit"
        brain.advance(state, {"commitment_made": True}, "yes I will", None)
        self.assertEqual(state["phase"], "concluded")

    def test_commit_attempts_exhaust(self):
        state = brain.new_state(_config())
        state["phase"] = "commit"
        for _ in range(brain.MAX_COMMIT):
            brain.advance(state, {"commitment_made": False}, "not sure", None)
        self.assertEqual(state["phase"], "concluded")


class PromptAssembly(unittest.TestCase):

    def test_guardrails_lead_the_prompt(self):
        system = brain.build_system("goal", {"clarifiers_left": 2}, _config())
        self.assertTrue(system.startswith("# Guardrails"),
                        "guardrails must lead the system prompt (CLAUDE.md §6)")

    def test_voice_style_is_present(self):
        system = brain.build_system("goal", {"clarifiers_left": 2}, _config())
        self.assertIn("Speaking, not writing", system)

    def test_opening_mandates_ai_disclosure(self):
        system = brain.build_system("goal", {}, _config(), opening=True)
        self.assertIn("you are Rudi, an AI assistant", system)
        self.assertIn("mandatory", system)

    def test_name_and_topic_reach_the_brief(self):
        system = brain.build_system("goal", {}, _config(user_name="Ada", topic="swimming"))
        self.assertIn("Ada", system)
        self.assertIn("swimming", system)

    def test_health_domain_pulls_in_coaching_guidance(self):
        system = brain.build_system("commit", {"goal_domain": "diabetes", "attempts_left": 5},
                                    _config())
        self.assertIn("T2D guidance", system)

    def test_time_pressure_forces_a_final_message(self):
        system = brain.build_system("commit", {"attempts_left": 5}, _config(max_minutes=12),
                                    elapsed_s=12 * 60 - 30)
        self.assertIn("FINAL message", system)

    def test_no_time_note_early_in_the_call(self):
        system = brain.build_system("commit", {"attempts_left": 5}, _config(max_minutes=12),
                                    elapsed_s=60)
        self.assertNotIn("out of time", system)


class Speakability(unittest.TestCase):

    def test_markdown_is_stripped_before_tts(self):
        self.assertEqual(brain._speakable("**Great** work on _that_ `goal`"),
                         "Great work on that goal")

    def test_envelope_survives_a_chatty_model(self):
        env = brain.parse_envelope('Sure! {"reply":"Hi there","signals":{"goal_status":"unclear"}}')
        self.assertEqual(env["reply"], "Hi there")
        self.assertEqual(env["signals"]["goal_status"], "unclear")

    def test_envelope_degrades_to_plain_text(self):
        env = brain.parse_envelope("no json at all")
        self.assertEqual(env["reply"], "no json at all")
        self.assertEqual(env["signals"], {})


class ConfigHandling(unittest.TestCase):

    def test_defaults_applied(self):
        cfg = app._clean_config({})
        self.assertEqual(cfg["language"], "en")
        self.assertEqual(cfg["start_phase"], "goal")
        self.assertEqual(cfg["max_minutes"], 12)

    def test_max_minutes_clamped(self):
        self.assertEqual(app._clean_config({"max_minutes": 999})["max_minutes"], 30)
        self.assertEqual(app._clean_config({"max_minutes": 0})["max_minutes"], 1)
        self.assertEqual(app._clean_config({"max_minutes": "junk"})["max_minutes"], 12)

    def test_bad_phase_falls_back(self):
        self.assertEqual(app._clean_config({"start_phase": "nonsense"})["start_phase"], "goal")

    def test_unknown_keys_ride_along(self):
        cfg = app._clean_config({"clinician_ref": "abc"})
        self.assertEqual(cfg["clinician_ref"], "abc")

    def test_asr_hint_carries_proper_nouns(self):
        hint = app._asr_hint(_config(user_name="Ada", topic="swimming"))
        self.assertIn("Ada", hint)
        self.assertIn("swimming", hint)

    def test_default_voice_is_not_a_stale_provider_name(self):
        """Switching TTS_PROVIDER must not leave the old provider's voice name in the config."""
        voice = app._clean_config({})["voice"]
        self.assertNotEqual(voice, "troy", "Groq voice leaked into the Piper default")
        self.assertTrue(voice)

    def test_sentence_gap_is_clamped(self):
        self.assertEqual(app._clean_config({"sentence_gap_ms": 99999})["sentence_gap_ms"], 2000)
        self.assertEqual(app._clean_config({"sentence_gap_ms": -5})["sentence_gap_ms"], 0)
        self.assertEqual(app._clean_config({"sentence_gap_ms": "x"})["sentence_gap_ms"], 300)

    def test_sentence_gap_reaches_the_synthesiser(self):
        _REPLIES[:] = [("Hello, this is Rudi, an AI assistant.", {})]
        _SYNTH_CALLS[:] = []
        _post({"action": "start", "config": _config(sentence_gap_ms=450)})
        self.assertEqual(_SYNTH_CALLS[-1]["sentence_gap_ms"], 450)
        self.assertEqual(_SYNTH_CALLS[-1]["voice"], "en_US-ryan-medium")


class CallRecord(unittest.TestCase):
    """A call must be fully reconstructable from S3 alone."""

    def test_full_call_is_written_and_finalised(self):
        _REPLIES[:] = [
            ("Hello, this is Rudi, an AI assistant.", {}),
            ("Twenty minutes of walking - have I got that right?",
             {"goal_status": "accepted", "goal": "walk 20 minutes", "goal_domain": "fitness"}),
            ("Great, I'll check in on Friday.", {"commitment_made": True}),
        ]

        started = _post({"action": "start", "config": _config()})
        self.assertTrue(started["ok"], started)
        call_id = started["call_id"]
        self.assertTrue(started["audio_b64"])

        audio = base64.b64encode(b"pretend-opus-audio-long-enough").decode()
        turn1 = _post({"action": "turn", "call_id": call_id,
                       "audio_b64": audio, "audio_mime": "audio/webm"})
        self.assertTrue(turn1["ok"], turn1)
        self.assertEqual(turn1["transcript"], "i want to walk more")
        self.assertEqual(turn1["phase"], "commit")

        turn2 = _post({"action": "turn", "call_id": call_id,
                       "audio_b64": audio, "audio_mime": "audio/webm"})
        self.assertTrue(turn2["ended"])

        ended = _post({"action": "end", "call_id": call_id, "reason": "hangup"})
        manifest = ended["manifest"]
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["totals"]["turns"], 2)
        self.assertEqual(manifest["outcome"]["goal"], "walk 20 minutes")
        self.assertIn("asr_ms", manifest["averages"])

        keys = _FAKE_S3._store[BUCKET].keys()
        self.assertIn("voice-bench/calls/%s/manifest.json" % call_id, keys)
        self.assertIn("voice-bench/calls/%s/turns/0001.json" % call_id, keys)
        self.assertIn("voice-bench/calls/%s/audio/0001-user.webm" % call_id, keys)
        self.assertTrue(any(k.startswith("voice-bench/index/") and call_id in k for k in keys))

    def test_audio_is_not_stored_when_retention_is_off(self):
        _REPLIES[:] = [("Hello, this is Rudi, an AI assistant.", {})]
        started = _post({"action": "start", "config": _config(store_audio=False)})
        call_id = started["call_id"]
        audio = base64.b64encode(b"pretend-opus-audio-long-enough").decode()
        _post({"action": "turn", "call_id": call_id, "audio_b64": audio,
               "audio_mime": "audio/webm"})
        keys = list(_FAKE_S3._store[BUCKET].keys())
        self.assertFalse([k for k in keys if k.startswith("voice-bench/calls/%s/audio/" % call_id)],
                         "store_audio=false must leave no audio behind")

    def test_unknown_call_is_rejected(self):
        self.assertFalse(_post({"action": "turn", "call_id": "nope", "audio_b64": "AAA"})["ok"])

    def test_tts_failure_degrades_to_text_and_still_records(self):
        """A dead voice must not destroy the call: text, phase and S3 record all survive."""
        _REPLIES[:] = [
            ("Hello, this is Rudi, an AI assistant.", {}),
            ("Twenty minutes then?",
             {"goal_status": "accepted", "goal": "walk 20 minutes", "goal_domain": "fitness"}),
        ]
        original = app.speech.synthesize

        def _boom(*_a, **_k):
            raise speech.SpeechError("model requires terms acceptance")

        app.speech.synthesize = _boom
        try:
            started = _post({"action": "start", "config": _config()})
            self.assertTrue(started["ok"], "a TTS outage must not fail the call")
            self.assertIsNone(started["audio_b64"])
            self.assertIn("terms acceptance", started["tts_error"])
            self.assertTrue(started["reply"])

            call_id = started["call_id"]
            audio = base64.b64encode(b"pretend-opus-audio-long-enough").decode()
            turn = _post({"action": "turn", "call_id": call_id,
                          "audio_b64": audio, "audio_mime": "audio/webm"})
            self.assertTrue(turn["ok"])
            self.assertEqual(turn["phase"], "commit", "the phase machine must still advance")
            self.assertIsNone(turn["audio_b64"])
        finally:
            app.speech.synthesize = original

        manifest = calllog.load(call_id)
        self.assertEqual(manifest["outcome"]["goal"], "walk 20 minutes")
        keys = _FAKE_S3._store[BUCKET].keys()
        self.assertIn("voice-bench/calls/%s/turns/0001.json" % call_id, keys)
        self.assertNotIn("voice-bench/calls/%s/audio/0001-rudi.wav" % call_id, keys)

    def test_empty_transcript_does_not_burn_a_phase_turn(self):
        _REPLIES[:] = [("Hello, this is Rudi, an AI assistant.", {})]
        started = _post({"action": "start", "config": _config()})
        call_id = started["call_id"]

        original = speech.transcribe
        app.speech.transcribe = lambda *a, **k: ("", 90)
        try:
            audio = base64.b64encode(b"pretend-silence-blob-here").decode()
            out = _post({"action": "turn", "call_id": call_id, "audio_b64": audio,
                         "audio_mime": "audio/webm"})
        finally:
            app.speech.transcribe = original

        self.assertTrue(out["ok"])
        self.assertTrue(out["empty"])
        self.assertFalse(out["ended"])
        self.assertEqual(calllog.load(call_id)["state"]["clarifiers_used"], 0)


class LeadSplit(unittest.TestCase):
    """Speaking the first sentence while the rest still renders is what turns a ~2s silence
    into a sub-second one."""

    def test_first_sentence_is_the_lead(self):
        lead, rest = app._split_lead(
            "That is really good going. How did the last one feel?")
        self.assertEqual(lead, "That is really good going.")
        self.assertEqual(rest, "How did the last one feel?")

    def test_tiny_opener_is_merged_forward(self):
        """A 0.6s clip followed by a fetch stutters more than it helps."""
        lead, rest = app._split_lead("Hi Filip. I am Rudi, an AI assistant. How are you?")
        self.assertEqual(lead, "Hi Filip. I am Rudi, an AI assistant.")
        self.assertEqual(rest, "How are you?")

    def test_single_sentence_has_no_remainder(self):
        lead, rest = app._split_lead("Twenty-five minutes, twice this week?")
        self.assertEqual(lead, "Twenty-five minutes, twice this week?")
        self.assertEqual(rest, "")

    def test_turn_returns_lead_audio_and_rest_text(self):
        _REPLIES[:] = [
            ("Hello there, this is Rudi speaking.", {}),
            ("That is good going. How did it feel?", {}),
        ]
        started = _post({"action": "start", "config": _config()})
        call_id = started["call_id"]
        audio = base64.b64encode(b"pretend-opus-audio-long-enough").decode()
        turn = _post({"action": "turn", "call_id": call_id,
                      "audio_b64": audio, "audio_mime": "audio/webm"})
        self.assertEqual(turn["rest_text"], "How did it feel?")
        self.assertEqual(_SYNTH_CALLS[-1]["text"], "That is good going.",
                         "only the lead should be synthesised on the turn")

    def test_speak_action_renders_the_remainder(self):
        _REPLIES[:] = [("Hello there, this is Rudi speaking.", {})]
        call_id = _post({"action": "start", "config": _config()})["call_id"]
        out = _post({"action": "speak", "call_id": call_id, "text": "How did it feel?"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["audio_b64"])
        self.assertEqual(_SYNTH_CALLS[-1]["text"], "How did it feel?")

    def test_speak_rejects_unknown_call(self):
        self.assertFalse(_post({"action": "speak", "call_id": "nope", "text": "hi"})["ok"])


class PaceControl(unittest.TestCase):
    """The client measures the patient's cadence and asks for a matching pace. Bounds are tight
    because Piper degrades outside them and exact mirroring is uncanny rather than warm."""

    def test_pace_is_clamped_to_a_safe_band(self):
        self.assertEqual(app._pace({"length_scale": 9.0}), 1.45)
        self.assertEqual(app._pace({"length_scale": 0.1}), 0.85)
        self.assertEqual(app._pace({"length_scale": 1.2}), 1.2)

    def test_absent_pace_leaves_the_voice_default(self):
        self.assertIsNone(app._pace({}))
        self.assertIsNone(app._pace({"length_scale": None}))
        self.assertIsNone(app._pace({"length_scale": "fast"}))

    def test_pace_reaches_the_synthesiser_on_a_turn(self):
        _REPLIES[:] = [("Hello there, this is Rudi.", {}), ("Good going. How did it feel?", {})]
        call_id = _post({"action": "start", "config": _config()})["call_id"]
        audio = base64.b64encode(b"pretend-opus-audio-long-enough").decode()
        _SYNTH_CALLS[:] = []
        _post({"action": "turn", "call_id": call_id, "audio_b64": audio,
               "audio_mime": "audio/webm", "length_scale": 1.25})
        self.assertEqual(_SYNTH_CALLS[-1]["length_scale"], 1.25)

    def test_remainder_keeps_the_same_pace(self):
        """The reply must not change speed halfway through, at the lead/rest seam."""
        _REPLIES[:] = [("Hello there, this is Rudi.", {})]
        call_id = _post({"action": "start", "config": _config()})["call_id"]
        _SYNTH_CALLS[:] = []
        _post({"action": "speak", "call_id": call_id, "text": "How did it feel?",
               "length_scale": 1.25})
        self.assertEqual(_SYNTH_CALLS[-1]["length_scale"], 1.25)


class CorsHeaders(unittest.TestCase):
    """The Function URL emits CORS headers itself. Emitting them from the handler as well made
    the browser see a duplicated Access-Control-Allow-Origin and refuse the response with a
    bare 'Failed to fetch'. Server-side callers never noticed, so only a browser caught it."""

    def test_handler_does_not_set_cors_headers(self):
        ev = {"body": json.dumps({"action": "end", "call_id": "nope"})}
        headers = {k.lower() for k in app.handler(ev, None)["headers"]}
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertIn("content-type", headers)


class SpeechHelpers(unittest.TestCase):

    def test_mime_to_extension(self):
        self.assertEqual(speech.ext_for_mime("audio/webm;codecs=opus"), "webm")
        self.assertEqual(speech.ext_for_mime("audio/wav"), "wav")
        self.assertEqual(speech.ext_for_mime("something/odd"), "webm")


if __name__ == "__main__":
    unittest.main()
