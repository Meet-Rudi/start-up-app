"""
meetrudi-call tests — a simulated Twilio ConversationRelay client, no network.

The point of these is that a real phone call is an expensive, slow and irreversible way to find
a bug. Everything except Twilio's own audio handling is exercised here: the wire protocol, the
consent and quiet-hour gates, the voicemail fallback, and a whole call driven turn by turn.

Run:  python -m unittest discover -s services/call/tests -v
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

BUCKET = "meetrudi-ai-data-test"
os.environ.setdefault("DATA_BUCKET", BUCKET)
os.environ.setdefault("WS_URL", "wss://example.execute-api.eu-central-1.amazonaws.com/live")

_FAKE_S3 = FakeS3()
_SENT: list = []          # frames pushed to Twilio
_TWILIO_CALLS: list = []  # REST calls to Twilio


class _FakeMgmt:
    def post_to_connection(self, ConnectionId, Data):  # noqa: N803
        _SENT.append(json.loads(Data.decode("utf-8")))


class _FakeSecrets:
    def get_secret_value(self, SecretId):  # noqa: N803
        if "twilio" in SecretId:
            return {"SecretString": json.dumps(
                {"account_sid": "AC123", "auth_token": "tok", "from_number": "+3212345678"})}
        return {"SecretString": json.dumps({"token": "dispatch-token"})}


boto3_stub = types.ModuleType("boto3")
boto3_stub.client = lambda name, *a, **k: (
    _FakeMgmt() if name == "apigatewaymanagementapi"
    else _FakeSecrets() if name == "secretsmanager"
    else _FAKE_S3)
sys.modules["boto3"] = boto3_stub

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
import relay      # noqa: E402
import ws         # noqa: E402
import dispatcher  # noqa: E402

_REPLIES: list = []


def _fake_generate(messages, json_mode=False):
    if not _REPLIES:
        return {"text": json.dumps({"reply": "Alright.", "signals": {}}), "model": "fake"}
    reply, signals = _REPLIES.pop(0)
    if json_mode:
        return {"text": json.dumps({"reply": reply, "signals": signals}), "model": "fake"}
    return {"text": reply, "model": "fake"}


brain.gateway.generate = _fake_generate
dispatcher._twilio_post = lambda path, form, creds: (
    _TWILIO_CALLS.append((path, form)) or {"sid": "CA999", "status": "queued"})


# Pinned to 14:00 Brussels. Every test that dials must be deterministic: an earlier version of
# this suite passed in the afternoon and failed after 21:30 purely because the quiet-hours gate
# reads the wall clock.
MIDDAY = datetime.datetime(2026, 8, 14, 12, 0, tzinfo=datetime.timezone.utc)


def _config(**over):
    cfg = {"language": "en", "user_name": "Filip", "topic": "daily walking",
           "to": "+32470000000", "consent_state": "granted", "status": "active",
           "start_phase": "goal", "max_minutes": 12}
    cfg.update(over)
    return cfg


# ------------------------------------------------------------------ simulated Twilio client

class Relay:
    """Drives the handler exactly as ConversationRelay would."""

    def __init__(self, call_id, connection_id="conn-1"):
        self.call_id = call_id
        self.connection_id = connection_id

    def _event(self, route, body=None):
        ev = {"requestContext": {"routeKey": route, "connectionId": self.connection_id}}
        if body is not None:
            ev["body"] = json.dumps(body)
        return ev

    def connect(self):
        return ws.handler(self._event("$connect"), None)

    def setup(self, **over):
        msg = {"type": "setup", "sessionId": "VX1", "callSid": "CA1",
               "from": "+3212345678", "to": "+32470000000", "direction": "outbound-api",
               "callStatus": "in-progress", "customParameters": {"call_id": self.call_id}}
        msg.update(over)
        return ws.handler(self._event("$default", msg), None)

    def prompt(self, text, last=True):
        return ws.handler(self._event(
            "$default", {"type": "prompt", "voicePrompt": text, "lang": "en-US", "last": last}),
            None)

    def interrupt(self, said, after_ms=460):
        return ws.handler(self._event("$default", {
            "type": "interrupt", "utteranceUntilInterrupt": said,
            "durationUntilInterruptMs": after_ms}), None)

    def disconnect(self):
        return ws.handler(self._event("$disconnect"), None)


def _spoken():
    return [f["token"] for f in _SENT if f.get("type") == "text"]


def _ended():
    return [f for f in _SENT if f.get("type") == "end"]


def _new_call(**over):
    _SENT.clear()
    _TWILIO_CALLS.clear()
    payload, _ = dispatcher.place_call(_config(**over), now=MIDDAY)
    return payload


# ------------------------------------------------------------------ tests

class Twiml(unittest.TestCase):

    def test_call_id_rides_through_as_a_custom_parameter(self):
        """The only thread linking the socket back to the patient's record."""
        twiml = relay.build_twiml("wss://x/live", "20260814-1-abc")
        self.assertIn('<Parameter name="call_id" value="20260814-1-abc"/>', twiml)
        self.assertIn('url="wss://x/live"', twiml)

    def test_barge_in_and_endpointing_are_configured(self):
        twiml = relay.build_twiml("wss://x/live", "c1")
        self.assertIn('interruptible="any"', twiml)
        self.assertIn('speechTimeout="700"', twiml)
        self.assertIn('reportInputDuringAgentSpeech="speech"', twiml)

    def test_no_canned_greeting(self):
        """Rudi's opening must be generated — it names the person and carries the disclosure."""
        self.assertNotIn("welcomeGreeting", relay.build_twiml("wss://x/live", "c1"))

    def test_free_text_is_escaped(self):
        twiml = relay.build_twiml("wss://x/live", "c1", hints='Filip, "knee" surgery & walking')
        self.assertNotIn('"knee"', twiml)
        self.assertIn("&amp;", twiml)


class Languages(unittest.TestCase):
    """Rudi has to speak the patient's language, and for the pilot that means Flemish."""

    def test_default_is_english(self):
        self.assertIn('language="en-US"', relay.build_twiml("wss://x/live", "c1"))

    def test_nl_resolves_to_flemish_not_netherlands_dutch(self):
        """Same rule as meetrudi-tts: the cohort is Belgian and the difference is audible."""
        twiml = relay.build_twiml("wss://x/live", "c1", language="nl")
        self.assertIn('language="nl-BE"', twiml)
        self.assertNotIn("nl-NL", twiml)

    def test_netherlands_dutch_still_reachable(self):
        self.assertIn('language="nl-NL"',
                      relay.build_twiml("wss://x/live", "c1", language="nl-NL"))

    def test_french_defaults_to_belgium(self):
        """Wallonia, not Paris."""
        self.assertIn('language="fr-BE"',
                      relay.build_twiml("wss://x/live", "c1", language="fr"))

    def test_unknown_language_falls_back_rather_than_failing(self):
        self.assertIn('language="en-US"',
                      relay.build_twiml("wss://x/live", "c1", language="klingon"))

    def test_non_english_leaves_the_voice_to_twilio(self):
        """Guessing a provider voice ID fails at dial time; an unset voice does not."""
        self.assertNotIn("voice=", relay.build_twiml("wss://x/live", "c1", language="nl"))

    def test_endpointing_is_eager_because_barge_in_covers_it(self):
        """The pause a caller feels is mostly this. It can be aggressive precisely because
        interruptible="any" makes an early reply recoverable — they just talk over it."""
        twiml = relay.build_twiml("wss://x/live", "c1")
        self.assertIn('speechTimeout="700"', twiml)
        self.assertIn('interruptible="any"', twiml)

    def test_shared_dials_survive_a_language_switch(self):
        twiml = relay.build_twiml("wss://x/live", "c1", language="nl")
        self.assertIn('interruptible="any"', twiml)
        self.assertIn('speechTimeout="700"', twiml)

    def test_per_call_override_wins(self):
        twiml = relay.build_twiml("wss://x/live", "c1", {"speechTimeout": "2500"}, language="nl")
        self.assertIn('speechTimeout="2500"', twiml)
        self.assertIn('language="nl-BE"', twiml)

    def test_dispatcher_passes_the_config_language_through(self):
        payload = _new_call(language="nl", topic="elke dag een half uur wandelen")
        twiml = json.loads(_FAKE_S3._store[BUCKET][
            "calls/%s/manifest.json" % payload["call_id"]].decode("utf-8"))["telephony"]["twiml"]
        self.assertIn('language="nl-BE"', twiml)


class Gates(unittest.TestCase):
    """Refusing to dial is the safe failure; these must be impossible to bypass by accident."""

    def test_no_consent_no_call(self):
        payload, _ = dispatcher.place_call(_config(consent_state="unknown"), now=MIDDAY)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["skipped"], "consent-not-granted")

    def test_revoked_consent_no_call(self):
        payload, _ = dispatcher.place_call(_config(consent_state="revoked"), now=MIDDAY)
        self.assertEqual(payload["skipped"], "consent-not-granted")

    def test_archived_contact_no_call(self):
        payload, _ = dispatcher.place_call(_config(status="archived"), now=MIDDAY)
        self.assertEqual(payload["skipped"], "contact-not-active")

    def test_missing_number_no_call(self):
        payload, _ = dispatcher.place_call(_config(to=""), now=MIDDAY)
        self.assertEqual(payload["skipped"], "no-destination-number")

    def test_quiet_hours_wrap_midnight(self):
        late = datetime.datetime(2026, 8, 14, 20, 30, tzinfo=datetime.timezone.utc)   # 22:30 BE
        early = datetime.datetime(2026, 8, 14, 4, 0, tzinfo=datetime.timezone.utc)    # 06:00 BE
        midday = datetime.datetime(2026, 8, 14, 12, 0, tzinfo=datetime.timezone.utc)  # 14:00 BE
        self.assertTrue(dispatcher.is_quiet(late))
        self.assertTrue(dispatcher.is_quiet(early))
        self.assertFalse(dispatcher.is_quiet(midday))

    def test_quiet_hours_block_the_call(self):
        late = datetime.datetime(2026, 8, 14, 20, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(dispatcher.gate(_config(), now=late), "quiet-hours")

    def test_quiet_hours_override_is_explicit_and_audited(self):
        """Testing happens at night. The gate still runs; the override is recorded so a call
        placed outside hours can never look like one placed inside them."""
        late = datetime.datetime(2026, 8, 14, 20, 30, tzinfo=datetime.timezone.utc)
        self.assertIsNone(dispatcher.gate(_config(override_quiet_hours=True), now=late))
        self.assertEqual(dispatcher.gate(_config(), now=late), "quiet-hours")

    def test_override_does_not_bypass_consent(self):
        """The escape hatch is for hours only — it must not unlock anything else."""
        payload, _ = dispatcher.place_call(
            _config(override_quiet_hours=True, consent_state="unknown"), now=MIDDAY)
        self.assertEqual(payload["skipped"], "consent-not-granted")

    def test_a_permitted_call_reaches_twilio(self):
        payload = _new_call()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["call_sid"], "CA999")
        path, form = _TWILIO_CALLS[-1]
        self.assertEqual(path, "Calls.json")
        self.assertEqual(form["To"], "+32470000000")
        self.assertIn("ConversationRelay", form["Twiml"])


class LiveCall(unittest.TestCase):

    def setUp(self):
        _REPLIES[:] = []
        self.payload = _new_call()
        self.relay = Relay(self.payload["call_id"])

    def test_rudi_speaks_first(self):
        """Outbound: the patient said nothing yet, so silence on answer is a failed call."""
        _REPLIES[:] = [("Hi Filip, I'm Rudi, an AI assistant.", {})]
        self.relay.connect()
        self.relay.setup()
        self.assertEqual(_spoken(), ["Hi Filip, I'm Rudi, an AI assistant."])

    def test_setup_links_the_socket_to_the_record(self):
        _REPLIES[:] = [("Hello.", {})]
        self.relay.setup()
        manifest = calllog.load(self.payload["call_id"])
        self.assertEqual(manifest["telephony"]["call_sid"], "CA1")
        self.assertEqual(manifest["totals"]["turns"], 0)

    def test_a_whole_call_runs_and_records(self):
        _REPLIES[:] = [
            ("Hi Filip, I'm Rudi, an AI assistant.", {}),
            ("Twenty minutes of walking, have I got that right?",
             {"goal_status": "accepted", "goal": "walk 20 minutes", "goal_domain": "fitness"}),
            ("Great, I'll check in on Friday.", {"commitment_made": True}),
        ]
        self.relay.connect()
        self.relay.setup()
        self.relay.prompt("I want to walk more")
        self.relay.prompt("Yes, that's right, I'll do it")

        manifest = calllog.load(self.payload["call_id"])
        self.assertEqual(manifest["outcome"]["goal"], "walk 20 minutes")
        self.assertEqual(manifest["state"]["phase"], "concluded")
        self.assertEqual(manifest["totals"]["turns"], 2)
        self.assertTrue(_ended(), "the engine concluding must hang the call up")
        self.assertEqual(_ended()[0]["type"], "end")
        self.assertEqual(calllog.load(self.payload["call_id"])["end_reason"], "engine-ended")

        keys = _FAKE_S3._store[BUCKET].keys()
        self.assertIn("calls/%s/turns/0001.json" % self.payload["call_id"], keys)

    def test_goodbye_is_queued_before_the_hangup(self):
        """`end` after the token, so Twilio speaks the closing rather than cutting it off."""
        _REPLIES[:] = [("Hello.", {}), ("Take care, goodbye.", {"commitment_made": True})]
        payload = _new_call(start_phase="commit")
        r = Relay(payload["call_id"], "conn-bye")
        r.setup()
        r.prompt("yes I will")
        kinds = [f["type"] for f in _SENT]
        self.assertLess(kinds.index("text"), kinds.index("end"))
        self.assertIn("Take care, goodbye.", _spoken())

    def test_interruption_is_recorded(self):
        _REPLIES[:] = [("Hello.", {})]
        self.relay.setup()
        self.relay.interrupt("Life is a complex set of", 460)
        manifest = calllog.load(self.payload["call_id"])
        self.assertEqual(len(manifest["interruptions"]), 1)
        self.assertEqual(manifest["interruptions"][0]["after_ms"], 460)

    def test_empty_prompt_costs_nothing(self):
        _REPLIES[:] = [("Hello.", {})]
        self.relay.setup()
        before = len(_SENT)
        self.relay.prompt("   ")
        self.assertEqual(len(_SENT), before)

    def test_disconnect_finalises_the_record(self):
        _REPLIES[:] = [("Hello.", {})]
        self.relay.setup()
        self.relay.disconnect()
        manifest = calllog.load(self.payload["call_id"])
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["end_reason"], "disconnected")

    def test_unknown_call_id_hangs_up_rather_than_improvising(self):
        _SENT.clear()
        Relay("no-such-call", "conn-x").setup()
        self.assertTrue(_ended())
        self.assertEqual(_spoken(), [])

    def test_a_broken_turn_does_not_drop_the_call(self):
        _REPLIES[:] = [("Hello.", {})]
        self.relay.setup()
        original = brain.turn
        brain.turn = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model exploded"))
        try:
            self.relay.prompt("are you there")
        finally:
            brain.turn = original
        self.assertIn(ws.FRIENDLY_ERROR, _spoken())
        self.assertFalse(_ended(), "a bad turn must not hang up on the patient")


class GreetThenDisclose(unittest.TestCase):
    """Nobody answers a phone in silence. The first live call showed Rudi delivering his whole
    opening — including the AI disclosure — straight over the patient saying "hello?", and then
    having to repeat it. The disclosure must land when they are listening, not when they are
    talking."""

    def test_opening_asks_for_a_greeting_only(self):
        system = brain.build_system("goal", {}, _config(), opening=True)
        self.assertIn("Say ONLY a short greeting", system)
        self.assertIn("hands the turn straight back", system)

    def test_opening_does_not_carry_the_disclosure(self):
        system = brain.build_system("goal", {}, _config(), opening=True)
        self.assertNotIn("You have not yet told them what you are", system)

    def test_first_real_turn_carries_the_disclosure(self):
        system = brain.build_system("goal", {"clarifiers_left": 2}, _config(), disclose=True)
        self.assertIn("say plainly that you are Rudi, an AI assistant", system)
        self.assertIn("mandatory", system)

    def test_disclosure_is_dropped_once_delivered(self):
        system = brain.build_system("goal", {"clarifiers_left": 2}, _config(), disclose=False)
        self.assertNotIn("You have not yet told them what you are", system)

    def test_state_tracks_the_disclosure_across_turns(self):
        _REPLIES[:] = [("Hello Filip?", {}),
                       ("I'm Rudi, an AI assistant. You wanted to walk more, is that right?", {}),
                       ("Good.", {})]
        payload = _new_call()
        r = Relay(payload["call_id"], "conn-disc")
        r.setup()
        self.assertFalse(calllog.load(payload["call_id"])["state"]["disclosed"],
                         "the greeting must not count as disclosure")
        r.prompt("Hello? Yes, speaking")
        self.assertTrue(calllog.load(payload["call_id"])["state"]["disclosed"])
        r.prompt("Yes that is right")
        self.assertTrue(calllog.load(payload["call_id"])["state"]["disclosed"],
                        "must stay disclosed, not re-fire every turn")


class Voicemail(unittest.TestCase):
    """Twilio's AMD does not cover Belgium, so this heuristic is the only guard."""

    def test_recognises_a_greeting(self):
        self.assertTrue(relay.looks_like_voicemail(
            "Hello, you have reached Filip. I am not available right now, "
            "please leave a message after the tone."))

    def test_recognises_dutch(self):
        self.assertTrue(relay.looks_like_voicemail(
            "Hallo, dit is de voicemail van Filip. Ik ben momenteel niet beschikbaar, "
            "laat een bericht achter na de toon."))

    def test_a_human_hello_is_never_voicemail(self):
        for human in ("Hello?", "Hi, yes?", "Hello, who is this please?",
                      "Yes hello, this is Filip speaking"):
            self.assertFalse(relay.looks_like_voicemail(human), human)

    def test_long_human_speech_without_the_phrases_is_not_voicemail(self):
        self.assertFalse(relay.looks_like_voicemail(
            "Oh hello yes, I was just in the garden so it took me a moment to get to the "
            "phone, sorry about that, how can I help you today?"))

    def test_call_hangs_up_on_voicemail_without_speaking(self):
        _REPLIES[:] = [("Hello.", {})]
        payload = _new_call()
        r = Relay(payload["call_id"], "conn-vm")
        r.setup()
        _SENT.clear()
        r.prompt("Hi, you've reached Filip, I'm not available, please leave a message "
                 "after the tone and I'll get back to you.")
        self.assertTrue(_ended())
        self.assertEqual(_spoken(), [], "never start coaching an answerphone")
        self.assertTrue(calllog.load(payload["call_id"])["telephony"]["voicemail_suspected"])

    def test_outcome_reason_survives_the_socket_closing(self):
        """$disconnect fires straight after our `end` and used to stamp "disconnected" over the
        real reason, which made voicemails uncountable from the index."""
        _REPLIES[:] = [("Hallo.", {})]
        payload = _new_call()
        r = Relay(payload["call_id"], "conn-vm3")
        r.setup()
        r.prompt("Hi, you've reached Filip, I'm not available, please leave a message "
                 "after the tone and I'll get back to you.")
        r.disconnect()
        manifest = calllog.load(payload["call_id"])
        self.assertEqual(manifest["end_reason"], "voicemail")
        self.assertEqual(manifest["status"], "completed")

    def test_only_the_first_utterance_is_screened(self):
        """Mid-call mention of voicemail is conversation, not an answerphone."""
        _REPLIES[:] = [("Hello.", {}), ("I see.", {})]
        payload = _new_call()
        r = Relay(payload["call_id"], "conn-vm2")
        r.setup()
        r.prompt("I want to walk more in the mornings")
        _SENT.clear()
        r.prompt("Sorry, I had to leave a message for my doctor after the tone earlier today")
        self.assertFalse(_ended())


class FailClosed(unittest.TestCase):
    """A missing secret is the likeliest thing to be wrong during setup, so it must say so
    rather than surfacing as an opaque 500 with an empty body."""

    def test_missing_dispatch_token_is_a_503_not_a_500(self):
        original = dispatcher._secrets
        dispatcher._secrets = types.SimpleNamespace(
            get_secret_value=lambda SecretId: (_ for _ in ()).throw(
                RuntimeError("Secrets Manager can't find the specified secret.")))
        dispatcher._cache.clear()
        try:
            res = dispatcher.handler(
                {"requestContext": {"http": {}}, "body": json.dumps({"config": {}})}, None)
        finally:
            dispatcher._secrets = original
            dispatcher._cache.clear()
        self.assertEqual(res["statusCode"], 503)
        self.assertIn("auth not configured", json.loads(res["body"])["error"])

    def test_missing_twilio_credentials_do_not_dial(self):
        original = dispatcher._secrets
        dispatcher._secrets = types.SimpleNamespace(
            get_secret_value=lambda SecretId: (_ for _ in ()).throw(
                RuntimeError("Secrets Manager can't find the specified secret.")))
        dispatcher._cache.clear()
        _TWILIO_CALLS.clear()
        try:
            payload, status = dispatcher.place_call(_config(), now=MIDDAY)
        finally:
            dispatcher._secrets = original
            dispatcher._cache.clear()
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])
        self.assertEqual(_TWILIO_CALLS, [], "must not attempt a dial without credentials")

    def test_wrong_dispatch_token_rejected(self):
        res = dispatcher.handler(
            {"requestContext": {"http": {}},
             "body": json.dumps({"token": "wrong", "config": _config()})}, None)
        self.assertEqual(res["statusCode"], 401)


class Protocol(unittest.TestCase):

    def test_malformed_frame_never_raises(self):
        self.assertEqual(relay.parse("not json")["type"], "unknown")
        self.assertEqual(relay.parse("[1,2,3]")["type"], "unknown")

    def test_text_frame_is_interruptible(self):
        frame = json.loads(relay.say("hello"))
        self.assertEqual(frame["type"], "text")
        self.assertTrue(frame["interruptible"])
        self.assertTrue(frame["last"])


if __name__ == "__main__":
    unittest.main()
