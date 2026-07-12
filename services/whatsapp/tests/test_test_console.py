"""
Tests for meetrudi-test-console-api — login, CRUD, engine send, soft-delete, export.

boto3 is stubbed to an in-memory FakeS3; the responder gateway is faked so no network/LLM is hit.
Test conversations use their own S3 prefix, isolated from live WhatsApp data. Synthetic only (§8).

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
os.environ["PSEUDONYMIZE_SALT"] = "test-salt"
# Direct credential override (no Secrets Manager in unit tests).
os.environ["TEST_CONSOLE_EMAIL"] = "tester@meetrudi.local"
os.environ["TEST_CONSOLE_PASSWORD"] = "s3cret"
os.environ["TEST_CONSOLE_TOKEN"] = "tok-123"

import store  # noqa: E402
import responder  # noqa: E402
import personality  # noqa: E402
import test_console  # noqa: E402

BUCKET = "meetrudi-ai-data-test"

# Seed prompt assets the engine reads + a personality for the dropdown/validation.
for key in ("prompts/rudi_guardrails.md", "prompts/rudi_learn_prompt_wa.md",
            "prompts/rudi_goal_prompt.md", "prompts/rudi_commit_prompt.md",
            "contexts/rudi-context.md"):
    _FAKE_S3.put_object(Bucket=BUCKET, Key=key, Body=("PROMPT " + key).encode())
_FAKE_S3.put_object(Bucket=BUCKET, Key="personalities/seed-rudi-v2/personality.json",
                    Body=json.dumps({"name": "Seed Rudi v2", "slug": "seed-rudi-v2",
                                     "ocean": {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19},
                                     "briefing": None}).encode())


def _fake_generate(messages, json_mode=False):
    return {"text": json.dumps({"reply": "Rudi says hi", "signals": {"lang": "en"}}), "model": "fake"}


def _event(method, path, body=None, query=None, token="tok-123"):
    headers = {"x-console-token": token} if token else {}
    return {"requestContext": {"http": {"method": method}}, "rawPath": path,
            "queryStringParameters": query or {}, "headers": headers,
            "body": json.dumps(body) if body is not None else None}


class TestConsoleTests(unittest.TestCase):
    def setUp(self):
        _FAKE_S3.__init__()
        # re-seed assets + personality (store reset above wipes them)
        for key in ("prompts/rudi_guardrails.md", "prompts/rudi_learn_prompt_wa.md",
                    "prompts/rudi_goal_prompt.md", "prompts/rudi_commit_prompt.md",
                    "contexts/rudi-context.md"):
            _FAKE_S3.put_object(Bucket=BUCKET, Key=key, Body=("PROMPT " + key).encode())
        _FAKE_S3.put_object(Bucket=BUCKET, Key="personalities/seed-rudi-v2/personality.json",
                            Body=json.dumps({"name": "Seed Rudi v2", "slug": "seed-rudi-v2",
                                             "ocean": {"O": 59, "C": 71, "E": 57, "A": 60, "N": 19},
                                             "briefing": None}).encode())
        # rebind shared module globals to our FakeS3 (other test files may have rebound them)
        test_console._s3 = _FAKE_S3
        test_console.STORE = store.ConversationStore(_FAKE_S3, BUCKET, prefix="test-conversations")
        responder.s3 = _FAKE_S3
        responder._asset_cache.clear()
        responder.gateway.generate = _fake_generate
        personality._s3 = _FAKE_S3
        personality._cache.clear()

    def _create(self, name="Ava test", persona=""):
        r = test_console.handler(_event("POST", "/conversations", body={"name": name, "persona": persona}), None)
        return r, json.loads(r["body"])

    # -- auth ------------------------------------------------------------
    def test_login_ok(self):
        r = test_console.handler(_event("POST", "/login", body={"email": "tester@meetrudi.local", "password": "s3cret"}, token=None), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"])["token"], "tok-123")

    def test_login_bad_password(self):
        r = test_console.handler(_event("POST", "/login", body={"email": "tester@meetrudi.local", "password": "nope"}, token=None), None)
        self.assertEqual(r["statusCode"], 401)

    def _bad_login(self):
        return test_console.handler(_event("POST", "/login",
                                           body={"email": "tester@meetrudi.local", "password": "nope"}, token=None), None)

    def _good_login(self):
        return test_console.handler(_event("POST", "/login",
                                           body={"email": "tester@meetrudi.local", "password": "s3cret"}, token=None), None)

    def test_lockout_after_10_failures(self):
        for i in range(9):
            r = self._bad_login()
            self.assertEqual(r["statusCode"], 401)
            self.assertEqual(json.loads(r["body"])["attempts_left"], 9 - i)
        r10 = self._bad_login()                     # 10th failure locks
        self.assertEqual(r10["statusCode"], 403)
        self.assertEqual(json.loads(r10["body"])["error"], "account_locked")
        # even the CORRECT password is refused while locked
        self.assertEqual(self._good_login()["statusCode"], 403)

    def test_success_resets_failure_streak(self):
        for _ in range(9):
            self._bad_login()
        self.assertEqual(self._good_login()["statusCode"], 200)   # reset streak at 9
        # a fresh failure starts the count over (would need 10 more to lock)
        r = self._bad_login()
        self.assertEqual(json.loads(r["body"])["attempts_left"], 9)

    def test_wrong_email_never_locks_real_account(self):
        for _ in range(15):
            test_console.handler(_event("POST", "/login",
                                        body={"email": "attacker@evil.test", "password": "x"}, token=None), None)
        self.assertEqual(self._good_login()["statusCode"], 200)   # real account still fine

    def test_admin_unlock_by_clearing_state(self):
        for _ in range(10):
            self._bad_login()
        self.assertEqual(self._good_login()["statusCode"], 403)   # locked
        _FAKE_S3.put_object(Bucket=BUCKET, Key=test_console.LOGIN_STATE_KEY, Body=b"{}")  # admin clears
        self.assertEqual(self._good_login()["statusCode"], 200)   # unlocked

    def test_routes_require_token(self):
        r = test_console.handler(_event("GET", "/conversations", token=None), None)
        self.assertEqual(r["statusCode"], 401)

    def test_health_open(self):
        r = test_console.handler(_event("GET", "/health", token=None), None)
        self.assertEqual(r["statusCode"], 200)

    # -- create / list ---------------------------------------------------
    def test_create_seeds_greeting_and_lists(self):
        r, body = self._create()
        self.assertEqual(r["statusCode"], 201)
        uid = body["conversation"]["user_id"]
        self.assertTrue(uid.startswith("test_"))
        # greeting seeded as an outbound message, no model call needed
        thr = json.loads(test_console.handler(_event("GET", "/conversations/%s/messages" % uid), None)["body"])
        self.assertEqual(len(thr["messages"]), 1)
        self.assertEqual(thr["messages"][0]["direction"], "out")
        # appears in the roster
        lst = json.loads(test_console.handler(_event("GET", "/conversations"), None)["body"])
        self.assertEqual([c["user_id"] for c in lst["conversations"]], [uid])

    def test_create_requires_name(self):
        r = test_console.handler(_event("POST", "/conversations", body={"name": "  "}), None)
        self.assertEqual(r["statusCode"], 400)

    def test_create_rejects_unknown_persona(self):
        r = test_console.handler(_event("POST", "/conversations", body={"name": "x", "persona": "ghost"}), None)
        self.assertEqual(r["statusCode"], 400)

    def test_test_conversations_isolated_from_live(self):
        # a test conversation must NOT show up in the live operator store prefix
        _, body = self._create()
        live = store.ConversationStore(_FAKE_S3, BUCKET)  # default "conversations/" prefix
        self.assertEqual(live.list_conversation_ids(), [])

    # -- send runs the engine --------------------------------------------
    def test_send_runs_engine_and_persists(self):
        _, body = self._create()
        uid = body["conversation"]["user_id"]
        r = test_console.handler(_event("POST", "/conversations/%s/messages" % uid, body={"text": "hello"}), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(json.loads(r["body"])["reply"], "Rudi says hi")
        thr = json.loads(test_console.handler(_event("GET", "/conversations/%s/messages" % uid), None)["body"])
        msgs = thr["messages"]
        # greeting + user turn + rudi turn (the user/reply pair can share a millisecond under the
        # instant fake gateway, so assert on content, not sub-ms ordering)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["direction"], "out")                       # greeting first
        self.assertTrue(any(m["direction"] == "in" and m["text"] == "hello" for m in msgs))
        self.assertTrue(any(m["direction"] == "out" and m["text"] == "Rudi says hi" for m in msgs))

    # -- soft delete -----------------------------------------------------
    def test_soft_delete_hides_but_keeps_data(self):
        _, body = self._create()
        uid = body["conversation"]["user_id"]
        d = test_console.handler(_event("POST", "/conversations/%s/delete" % uid), None)
        self.assertEqual(d["statusCode"], 200)
        lst = json.loads(test_console.handler(_event("GET", "/conversations"), None)["body"])
        self.assertEqual(lst["conversations"], [])          # hidden from view
        self.assertIsNotNone(test_console.STORE.get_meta(uid))  # data retained

    # -- export ----------------------------------------------------------
    def test_export_single(self):
        _, body = self._create("Ben test")
        uid = body["conversation"]["user_id"]
        r = test_console.handler(_event("GET", "/conversations/%s/export" % uid), None)
        b = json.loads(r["body"])
        self.assertTrue(b["filename"].startswith("Conversation_Ben_test_"))
        self.assertIn("# Conversation — Ben test", b["markdown"])

    def test_export_all_has_pagebreaks(self):
        self._create("A one")
        self._create("B two")
        r = test_console.handler(_event("GET", "/export", query={"scope": "all"}), None)
        b = json.loads(r["body"])
        self.assertEqual(b["count"], 2)
        self.assertIn("page-break-after", b["markdown"])
        self.assertTrue(b["filename"].startswith("Conversation_Export_"))

    def test_export_interval_filters_by_launch_date(self):
        _, body = self._create("Inrange")
        uid = body["conversation"]["user_id"]
        # force a known created_at outside a narrow window, then query a window that excludes it
        meta = test_console.STORE.get_meta(uid)
        meta.created_at = "2025-05-05T10:00:00+00:00"
        test_console.STORE.put_meta(meta)
        r = test_console.handler(_event("GET", "/export", query={"scope": "interval", "from": "2026-01-01", "to": "2026-12-31"}), None)
        self.assertEqual(json.loads(r["body"])["count"], 0)
        r2 = test_console.handler(_event("GET", "/export", query={"scope": "interval", "from": "2025-01-01", "to": "2025-12-31"}), None)
        self.assertEqual(json.loads(r2["body"])["count"], 1)

    def test_export_interval_bad_date(self):
        r = test_console.handler(_event("GET", "/export", query={"scope": "interval", "from": "nope", "to": "2026-01-01"}), None)
        self.assertEqual(r["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
