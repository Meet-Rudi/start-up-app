"""
meetrudi-call-status tests — Twilio status callbacks, no network.

The rule under test is one the tester console depends on: Rudi never talks to an answering
machine, and an attempt that met one is not deducted from a tester's five calls. That means the
manifest this handler writes has to distinguish "voicemail answered" from "a person answered"
with no ambiguity.

Run:  python -m unittest discover -s services/call/tests -v
"""

from __future__ import annotations

import os
import sys
import json
import types
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from fake_s3 import FakeS3  # noqa: E402

BUCKET = "meetrudi-ai-data-test"
os.environ.setdefault("DATA_BUCKET", BUCKET)

_FAKE_S3 = FakeS3()
_HANGUPS: list = []


class _FakeSecrets:
    def get_secret_value(self, SecretId):  # noqa: N803
        return {"SecretString": json.dumps(
            {"account_sid": "AC123", "auth_token": "tok", "from_number": "+3212345678"})}


# Only install a boto3 double if nothing else already did. test_call.py hands its own doubles to
# ws.py and dispatcher.py; replacing its stub here would leave those subjects wired to clients
# their assertions never see.
if "boto3" not in sys.modules:
    _boto3 = types.ModuleType("boto3")
    _boto3.client = lambda name, *a, **k: (
        _FakeSecrets() if name == "secretsmanager" else _FAKE_S3)
    sys.modules["boto3"] = _boto3

import calllog  # noqa: E402
import status  # noqa: E402

# Share whichever S3 double calllog already holds instead of forcing our own onto it — the
# handler reads and writes manifests through calllog, so binding to its client keeps this file
# consistent with itself and leaves test_call.py's wiring untouched.
_FAKE_S3 = calllog.s3

# `status` is this file's subject alone, so pointing its secrets client and hang-up at doubles
# affects nothing else.
status._secrets = _FakeSecrets()
status._cache.clear()
status.hangup = lambda call_sid, creds: (_HANGUPS.append(call_sid), True)[1]


def event_for(call_id, **form):
    body = urllib.parse.urlencode(form)
    return {"queryStringParameters": {"call_id": call_id},
            "body": body, "isBase64Encoded": False,
            "requestContext": {"http": {"method": "POST"}}}


def seed_manifest(call_id="call_1", **extra):
    manifest = {"call_id": call_id, "schema": 1, "status": "in_progress",
                "started_at": calllog.iso(), "ended_at": None, "end_reason": None,
                "totals": {"turns": 0}, "telephony": {"call_sid": "CA123"}}
    manifest.update(extra)
    _FAKE_S3.put_object(Bucket=BUCKET, Key="calls/%s/manifest.json" % call_id,
                        Body=json.dumps(manifest).encode())
    return manifest


def load(call_id="call_1"):
    raw = _FAKE_S3.get_object(Bucket=BUCKET, Key="calls/%s/manifest.json" % call_id)["Body"].read()
    return json.loads(raw)


class TestApplyStatus(unittest.TestCase):
    """apply_status is pure, so the decision table can be checked without any I/O."""

    def test_every_machine_verdict_is_treated_as_voicemail(self):
        for verdict in ("machine_start", "machine_end_beep", "machine_end_silence",
                        "machine_end_other", "fax"):
            manifest, machine = status.apply_status(
                {"telephony": {}}, {"AnsweredBy": verdict, "CallStatus": "in-progress"})
            self.assertTrue(machine, verdict)
            self.assertEqual(manifest["end_reason"], "voicemail")
            self.assertTrue(manifest["telephony"]["machine_detected"])

    def test_a_human_answer_is_left_alone(self):
        manifest, machine = status.apply_status(
            {"telephony": {}}, {"AnsweredBy": "human", "CallStatus": "in-progress"})
        self.assertFalse(machine)
        self.assertNotIn("machine_detected", manifest["telephony"])
        self.assertIsNone(manifest.get("end_reason"))

    def test_unknown_answer_is_not_treated_as_a_machine(self):
        # Twilio reports "unknown" when detection times out. Hanging up on a real person because
        # AMD was unsure would be far worse than talking to a voicemail.
        _, machine = status.apply_status({"telephony": {}}, {"AnsweredBy": "unknown"})
        self.assertFalse(machine)

    def test_terminal_statuses_close_the_manifest(self):
        for call_status in ("completed", "busy", "failed", "no-answer", "canceled"):
            manifest, _ = status.apply_status({"telephony": {}}, {"CallStatus": call_status})
            self.assertEqual(manifest["status"], "completed", call_status)
            self.assertTrue(manifest["ended_at"])

    def test_in_flight_statuses_do_not_close_it(self):
        for call_status in ("initiated", "ringing", "in-progress"):
            manifest, _ = status.apply_status({"telephony": {}, "status": "in_progress"},
                                              {"CallStatus": call_status})
            self.assertEqual(manifest["status"], "in_progress", call_status)

    def test_duration_is_recorded_and_garbage_ignored(self):
        manifest, _ = status.apply_status({"telephony": {}}, {"CallDuration": "137"})
        self.assertEqual(manifest["telephony"]["duration_s"], 137)
        manifest, _ = status.apply_status({"telephony": {}}, {"CallDuration": "not-a-number"})
        self.assertNotIn("duration_s", manifest["telephony"])

    def test_an_existing_end_reason_is_not_overwritten(self):
        manifest, _ = status.apply_status({"telephony": {}, "end_reason": "goodbye"},
                                          {"CallStatus": "completed"})
        self.assertEqual(manifest["end_reason"], "goodbye")


class TestHandler(unittest.TestCase):
    def setUp(self):
        # Deliberately NOT clearing the shared store: test_call.py seeds prompt objects into it
        # at import time. Each test uses its own call id instead, which is closer to reality
        # anyway — production never has an empty bucket either.
        _HANGUPS.clear()

    def test_voicemail_triggers_a_hangup_and_is_recorded(self):
        seed_manifest()
        resp = status.handler(event_for("call_1", AnsweredBy="machine_start",
                                        CallStatus="in-progress", CallSid="CA123"), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertTrue(json.loads(resp["body"])["machine"])
        self.assertEqual(_HANGUPS, ["CA123"])

        manifest = load()
        self.assertTrue(manifest["telephony"]["hung_up_on_machine"])
        self.assertEqual(manifest["telephony"]["answered_by"], "machine_start")
        self.assertEqual(manifest["end_reason"], "voicemail")

    def test_a_human_call_is_never_hung_up(self):
        seed_manifest()
        status.handler(event_for("call_1", AnsweredBy="human", CallStatus="in-progress"), None)
        self.assertEqual(_HANGUPS, [])
        self.assertEqual(load()["telephony"]["answered_by"], "human")

    def test_completed_call_lands_a_terminal_manifest(self):
        seed_manifest()
        status.handler(event_for("call_1", CallStatus="completed", CallDuration="212"), None)
        manifest = load()
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(manifest["telephony"]["call_status"], "completed")
        self.assertEqual(manifest["telephony"]["duration_s"], 212)

    def test_no_answer_is_recorded_without_a_hangup(self):
        seed_manifest()
        status.handler(event_for("call_1", CallStatus="no-answer"), None)
        self.assertEqual(_HANGUPS, [])
        self.assertEqual(load()["telephony"]["call_status"], "no-answer")

    def test_an_unknown_call_id_writes_nothing(self):
        # A guessed callback must not be able to conjure a call record into existence.
        resp = status.handler(event_for("call_made_up", CallStatus="completed"), None)
        self.assertEqual(resp["statusCode"], 404)
        self.assertIsNone(calllog.load("call_made_up"))

    def test_a_missing_call_id_is_a_bad_request(self):
        resp = status.handler({"body": "CallStatus=completed",
                               "queryStringParameters": {}}, None)
        self.assertEqual(resp["statusCode"], 400)

    def test_repeated_callbacks_are_safe(self):
        seed_manifest()
        for _ in range(3):
            status.handler(event_for("call_1", CallStatus="completed"), None)
        self.assertEqual(load()["status"], "completed")


class TestDispatcherWiring(unittest.TestCase):
    """The dispatcher must actually ask Twilio for machine detection, or none of the above runs.

    Asserted against the real form posted to Twilio rather than against the source, so a rename
    or a refactor that quietly drops the parameter still fails here.
    """

    def setUp(self):
        os.environ.setdefault("WS_URL", "wss://example.execute-api.eu-central-1.amazonaws.com/live")
        import dispatcher
        self.dispatcher = dispatcher
        self.posted = []
        self._real_post = dispatcher._twilio_post
        dispatcher._twilio_post = lambda path, form, creds: (
            self.posted.append(form), {"sid": "CA999", "status": "queued"})[1]

    def tearDown(self):
        self.dispatcher._twilio_post = self._real_post

    def _config(self, **kw):
        base = {"to": "+32479123456", "consent_state": "granted", "status": "active",
                "override_quiet_hours": True, "user_name": "Marieke",
                "timezone": "Europe/Brussels"}
        base.update(kw)
        return base

    def test_machine_detection_is_requested_when_the_config_asks(self):
        payload, code = self.dispatcher.place_call(self._config(machine_detection=True))
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        form = self.posted[0]
        self.assertEqual(form["MachineDetection"], "Enable")
        self.assertIn("MachineDetectionTimeout", form)
        self.assertEqual(form["To"], "+32479123456")

    def test_machine_detection_is_absent_unless_asked_for(self):
        self.dispatcher.place_call(self._config())
        self.assertNotIn("MachineDetection", self.posted[0])


if __name__ == "__main__":
    unittest.main()
