"""
Router + send-policy tests for meetrudi-wa-console-api (Block 2/5) — no boto3, no network.

boto3 and provider are stubbed in sys.modules before import so the handler can be exercised
against the in-memory FakeS3 and a fake Twilio provider. Synthetic data only.

Run:  python -m unittest discover -s services/whatsapp/tests -v
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

_FAKE_S3 = FakeS3()
_SENT: list[dict] = []


def _install_stubs():
    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = lambda name, *a, **k: _FAKE_S3  # every client() → shared FakeS3
    sys.modules["boto3"] = boto3_stub

    provider_stub = types.ModuleType("provider")

    def send_text(to_phone, body):
        _SENT.append({"to": to_phone, "body": body})
        return {"sid": "SMfake123", "status": "queued"}

    provider_stub.send_text = send_text
    sys.modules["provider"] = provider_stub


_install_stubs()
os.environ["DATA_BUCKET"] = "meetrudi-ai-data-test"
os.environ["PSEUDONYMIZE_SALT"] = "test-salt"

import store  # noqa: E402
import console_api  # noqa: E402


def _iso(y, mo, d, h=12, mi=0):
    return store.to_iso(datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.timezone.utc))


def _event(method, path, body=None, query=None, headers=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "queryStringParameters": query or {},
        "headers": headers or {},
        "body": json.dumps(body) if body is not None else None,
    }


class ConsoleApiTests(unittest.TestCase):
    def setUp(self):
        _FAKE_S3.__init__()          # reset store between tests
        _SENT.clear()
        console_api.CONSOLE_AUTH_TOKEN = ""
        console_api.personality._s3 = _FAKE_S3   # rebind (another test file may have rebound it)
        console_api.personality._cache.clear()
        self.uid = store.user_id("+32470000000", "test-salt")

    def _seed_inbound(self, at, text="hi", uid=None):
        uid = uid or self.uid
        console_api.STORE.record_inbound(
            uid, "+32470000000",
            store.Message(id="SM" + at[-5:], direction="in", text=text, at=at))

    # -- health / auth ---------------------------------------------------
    def test_health_open(self):
        r = console_api.handler(_event("GET", "/health"), None)
        self.assertEqual(r["statusCode"], 200)

    def test_auth_required_when_token_set(self):
        console_api.CONSOLE_AUTH_TOKEN = "secret"
        r = console_api.handler(_event("GET", "/conversations"), None)
        self.assertEqual(r["statusCode"], 401)
        r2 = console_api.handler(_event("GET", "/conversations", headers={"x-console-token": "secret"}), None)
        self.assertEqual(r2["statusCode"], 200)

    # -- roster / thread -------------------------------------------------
    def test_fail_closed_when_auth_required_but_unset(self):
        console_api.CONSOLE_AUTH_TOKEN = ""
        console_api.REQUIRE_AUTH = True
        try:
            r = console_api.handler(_event("GET", "/conversations"), None)
            self.assertEqual(r["statusCode"], 401)   # never expose PII open
        finally:
            console_api.REQUIRE_AUTH = False

    def test_roster(self):
        self._seed_inbound(_iso(2026, 7, 1, 10, 0))
        r = console_api.handler(_event("GET", "/conversations"), None)
        body = json.loads(r["body"])
        self.assertEqual(len(body["conversations"]), 1)
        self.assertEqual(body["conversations"][0]["user_id"], self.uid)
        self.assertEqual(body["conversations"][0]["unread_count"], 1)

    def test_thread_and_cursor(self):
        self._seed_inbound(_iso(2026, 7, 1, 10, 0), "first")
        r = console_api.handler(_event("GET", "/conversations/%s/messages" % self.uid), None)
        body = json.loads(r["body"])
        self.assertEqual([m["text"] for m in body["messages"]], ["first"])
        self.assertTrue(body["cursor"])

    def test_thread_unknown_404(self):
        r = console_api.handler(_event("GET", "/conversations/wa_nope/messages"), None)
        self.assertEqual(r["statusCode"], 404)

    # -- send: in-window vs out-of-window (§3, §8 mandatory) -------------
    def test_send_in_window(self):
        recent = store.to_iso(store.now_dt())   # window open now
        self._seed_inbound(recent)
        r = console_api.handler(
            _event("POST", "/conversations/%s/messages" % self.uid,
                   body={"text": "hello there", "operator_id": "op-anna"}), None)
        self.assertEqual(r["statusCode"], 201)
        self.assertEqual(len(_SENT), 1)
        self.assertEqual(_SENT[0]["to"], "+32470000000")
        # outbound persisted + unread cleared
        self.assertEqual(console_api.STORE.get_meta(self.uid).unread_count, 0)
        self.assertEqual(console_api.STORE.get_meta(self.uid).last_direction, "out")

    def test_send_out_of_window_blocked(self):
        self._seed_inbound(_iso(2026, 1, 1, 10, 0))   # long ago → window closed
        r = console_api.handler(
            _event("POST", "/conversations/%s/messages" % self.uid, body={"text": "hi"}), None)
        self.assertEqual(r["statusCode"], 409)
        self.assertEqual(json.loads(r["body"])["error"], "out_of_window")
        self.assertEqual(len(_SENT), 0)   # nothing sent to Twilio

    def test_send_empty_rejected(self):
        recent = store.to_iso(store.now_dt())
        self._seed_inbound(recent)
        r = console_api.handler(
            _event("POST", "/conversations/%s/messages" % self.uid, body={"text": "   "}), None)
        self.assertEqual(r["statusCode"], 400)

    def test_mark_read(self):
        self._seed_inbound(_iso(2026, 7, 1, 10, 0))
        console_api.handler(_event("POST", "/conversations/%s/read" % self.uid), None)
        self.assertEqual(console_api.STORE.get_meta(self.uid).unread_count, 0)

    def test_keepwarm_toggle(self):
        self._seed_inbound(store.to_iso(store.now_dt()))
        r = console_api.handler(
            _event("POST", "/conversations/%s/keepwarm" % self.uid, body={"enabled": False}), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertFalse(json.loads(r["body"])["keep_warm"])
        self.assertFalse(console_api.STORE.get_meta(self.uid).keep_warm)

    # -- personality (operator-chosen persona per conversation) ----------
    def _seed_personality(self, slug="seed-rudi-v2"):
        doc = {"name": "Seed Rudi v2", "slug": slug, "version": 1,
               "ocean": {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19}, "briefing": None}
        _FAKE_S3.put_object(Bucket="meetrudi-ai-data-test",
                            Key="personalities/%s/personality.json" % slug,
                            Body=json.dumps(doc).encode())
        console_api.personality._cache.clear()

    def test_list_personalities(self):
        self._seed_personality()
        r = console_api.handler(_event("GET", "/personalities"), None)
        self.assertEqual(r["statusCode"], 200)
        body = json.loads(r["body"])
        self.assertEqual(body["default"], "seed-rudi-v2")
        self.assertEqual([p["slug"] for p in body["personalities"]], ["seed-rudi-v2"])

    def test_roster_row_includes_persona(self):
        self._seed_inbound(_iso(2026, 7, 1, 10, 0))
        row = json.loads(console_api.handler(_event("GET", "/conversations"), None)["body"])["conversations"][0]
        self.assertEqual(row["persona"], "")                       # none chosen yet
        self.assertEqual(row["persona_effective"], "seed-rudi-v2")  # falls back to default

    def test_set_personality(self):
        self._seed_personality()
        self._seed_inbound(store.to_iso(store.now_dt()))
        r = console_api.handler(
            _event("POST", "/conversations/%s/personality" % self.uid, body={"slug": "seed-rudi-v2"}), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(console_api.STORE.get_meta(self.uid).persona, "seed-rudi-v2")

    def test_set_unknown_personality_rejected(self):
        self._seed_personality()
        self._seed_inbound(store.to_iso(store.now_dt()))
        r = console_api.handler(
            _event("POST", "/conversations/%s/personality" % self.uid, body={"slug": "ghost"}), None)
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(console_api.STORE.get_meta(self.uid).persona, "")  # unchanged

    def test_reset_personality_to_default(self):
        self._seed_personality()
        self._seed_inbound(store.to_iso(store.now_dt()))
        console_api.STORE.set_persona(self.uid, "seed-rudi-v2")
        r = console_api.handler(
            _event("POST", "/conversations/%s/personality" % self.uid, body={"slug": ""}), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(console_api.STORE.get_meta(self.uid).persona, "")

    def test_options_ok_and_no_manual_cors(self):
        # CORS is emitted by the Function URL, not the handler (avoids duplicate ACAO headers).
        r = console_api.handler(_event("OPTIONS", "/conversations"), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertNotIn("Access-Control-Allow-Origin", r["headers"])


if __name__ == "__main__":
    unittest.main()
