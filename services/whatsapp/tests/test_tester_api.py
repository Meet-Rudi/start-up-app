"""
Router, auth and gate tests for meetrudi-tester-api — no boto3, no network, synthetic data only.

boto3, the AI engine and the outbound call dispatcher are all stubbed before import, so the
handler runs end to end against in-memory doubles. What is asserted here is the set of rules a
regression would actually hurt someone with:

  - a public registration endpoint that validates properly and can't be walked for names
  - single-use verification links, lockout, and sessions that expire on idle
  - the number is hard-locked to the one captured at registration
  - the call gates: paused, spent, quiet hours, daily quota, one-at-a-time queue
  - only a CONNECTED call is deducted from a tester's five
  - the assigned call goal never reaches the tester's browser

Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import os
import io
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
_FAKE_S3 = FakeS3()
_SECRETS = {"meetrudi/tester-console/admin": json.dumps({"password": "adm1n-secret"}),
            "meetrudi/call/dispatch-token": json.dumps({"token": "dispatch-tok"})}
_MAILS: list[dict] = []
_DIALED: list[dict] = []


class _FakeSecrets:
    def get_secret_value(self, SecretId):
        if SecretId not in _SECRETS:
            raise KeyError(SecretId)
        return {"SecretString": _SECRETS[SecretId]}


class _FakeSes:
    def send_email(self, **kw):
        _MAILS.append(kw)
        return {"MessageId": "msg-%d" % len(_MAILS)}


def _client(name, *a, **k):
    """Permissive on purpose. This module shares a process with the rest of the suite, so an
    unknown client name must not explode for whichever test file imports next."""
    return {"secretsmanager": _FakeSecrets(), "ses": _FakeSes()}.get(name, _FAKE_S3)


# Only install a boto3 double if nothing else already did. Replacing a sibling test module's
# stub is how you get a green file and a red suite — the doubles the other modules handed to
# their own subjects would quietly stop matching.
if "boto3" not in sys.modules:
    _boto3 = types.ModuleType("boto3")
    _boto3.client = _client
    sys.modules["boto3"] = _boto3

os.environ["DATA_BUCKET"] = BUCKET
os.environ["PSEUDONYMIZE_SALT"] = "test-salt"
os.environ["TESTER_PBKDF2_ROUNDS"] = "1000"
os.environ["TESTER_MAIL_FROM"] = "Rudi test <test@meetrudi.eu>"
os.environ["TESTER_CONSOLE_BASE"] = "https://example.test/tester-console"
os.environ["CALL_DISPATCH_URL"] = "https://dispatch.test/"
os.environ["TESTER_WA_NUMBER"] = "+32460221109"
os.environ["TESTER_WA_JOIN_PHRASE"] = "join olive-tiger"

import store  # noqa: E402
import tester_store as ts  # noqa: E402
import tester_api as api  # noqa: E402


# The doubles are attached to tester_api's own globals rather than to sys.modules. The real
# `responder`/`gateway` modules stay untouched for every other test file in the suite, and the
# engine is exercised by its own tests — here it only has to be deterministic.
class _FakeResponder:
    @staticmethod
    def respond(state, user_text, locale="en", personality_block=""):
        if not state:
            return ("Hi, Rudi here. I'm an AI, not a person.", {"phase": "learn"},
                    {"phase": "learn", "lang": locale})
        return ("You said: " + user_text, {"phase": "goal"}, {"phase": "goal", "lang": locale})


class _FakePersonality:
    DEFAULT_SLUG = "seed-rudi-v2"

    @staticmethod
    def resolve_block(slug):
        return ""


api._s3 = _FAKE_S3
api._secrets = _FakeSecrets()
api._ses = _FakeSes()
api.responder = _FakeResponder
api.personality = _FakePersonality


# --------------------------------------------------------------------------- helpers
def call(method, path, body=None, token="", query=None):
    event = {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "headers": {"x-tester-token": token} if token else {},
        "queryStringParameters": query or {},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    resp = api.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


GOOD_REG = {
    "first_name": "Marieke", "last_name": "Dhondt",
    "email": "marieke@example.be", "phone": "0479 12 34 56",
    "locale": "nl-BE", "help_areas": ["Meer bewegen"], "goal": "Walk 30 minutes a day",
    "consent_health": True, "consent_recording": True, "whatsapp_confirmed": True,
}


def reset_world():
    _FAKE_S3._store.clear()
    _MAILS.clear()
    _DIALED.clear()
    api._cache.clear()
    api.STORE = ts.TesterStore(_FAKE_S3, BUCKET)
    api.CHAT = store.ConversationStore(_FAKE_S3, BUCKET, prefix="tester-conversations")
    fake_dispatch(ok=True)


def fake_dispatch(ok=True, skipped=None, call_id="call_test_1"):
    """Replace the one outbound HTTP call the API makes, and record what it was asked to dial."""
    def urlopen(req, timeout=None):
        payload = json.loads(req.data.decode("utf-8"))
        _DIALED.append(payload)
        body = ({"ok": True, "call_id": call_id} if ok
                else {"ok": False, "skipped": skipped or "quiet-hours"})

        class _R:
            def read(self_inner):
                return json.dumps(body).encode()

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

        return _R()

    api.urllib.request.urlopen = urlopen


def register_and_activate(email="marieke@example.be", password="Test1234"):
    """Walk a tester through the real flow and return (tester_id, session_token)."""
    payload = dict(GOOD_REG, email=email)
    call("POST", "/register", payload)
    tid = ts.tester_id(email, "test-salt")
    token = api.STORE.issue_link(tid, "verify")
    status, data = call("POST", "/set-password", {"token": token, "password": password})
    assert status == 200, data
    return tid, data["session"]


def finish_call(call_id, answered_by="human", call_status="completed", turns=3):
    """Write the manifest the call service would have written, so /call/status can settle."""
    _FAKE_S3.put_object(Bucket=BUCKET, Key="calls/%s/manifest.json" % call_id,
                        Body=json.dumps({
                            "call_id": call_id, "status": "completed",
                            "totals": {"turns": turns},
                            "telephony": {"answered_by": answered_by, "call_status": call_status},
                        }).encode())


# --------------------------------------------------------------------------- tests
class TestValidation(unittest.TestCase):
    def setUp(self):
        reset_world()

    def test_plausible_names_pass_and_mash_fails(self):
        for good in ("Marieke", "D'Hondt", "Van der Meer", "Éloïse", "Jo"):
            self.assertTrue(api.name_is_plausible(good), good)
        for bad in ("aaaaaaaa", "a", "", "123", "Marieke9", "   ", "xxx"):
            self.assertFalse(api.name_is_plausible(bad), repr(bad))

    def test_belgian_mobiles_normalise_and_others_are_refused(self):
        self.assertEqual(api.normalize_phone("0479 12 34 56"), "+32479123456")
        self.assertEqual(api.normalize_phone("+32 479 12 34 56"), "+32479123456")
        self.assertEqual(api.normalize_phone("0479.12.34.56"), "+32479123456")
        for bad in ("02 123 45 67", "+31 6 12345678", "0412345678", "", "0479123", "abc"):
            self.assertEqual(api.normalize_phone(bad), "", bad)

    def test_registration_rejects_a_landline(self):
        status, data = call("POST", "/register", dict(GOOD_REG, phone="02 123 45 67"))
        self.assertEqual(status, 400)
        self.assertEqual(data["fields"]["phone"], "not_belgian_mobile")

    def test_registration_rejects_keyboard_mash(self):
        status, data = call("POST", "/register", dict(GOOD_REG, last_name="aaaaaaaa"))
        self.assertEqual(status, 400)
        self.assertIn("last_name", data["fields"])

    def test_both_consents_are_mandatory(self):
        status, data = call("POST", "/register", dict(GOOD_REG, consent_health=False))
        self.assertEqual(status, 400)
        self.assertIn("consent_health", data["fields"])
        status, data = call("POST", "/register", dict(GOOD_REG, consent_recording=False))
        self.assertEqual(status, 400)
        self.assertIn("consent_recording", data["fields"])

    def test_honeypot_submission_is_dropped_without_creating_anything(self):
        status, _ = call("POST", "/register", dict(GOOD_REG, website="http://spam"))
        self.assertEqual(status, 400)
        self.assertEqual(api.STORE.count(), 0)

    def test_registration_stores_the_profile_and_sends_one_mail(self):
        status, data = call("POST", "/register", GOOD_REG)
        self.assertEqual(status, 201)
        self.assertTrue(data["mail_sent"])
        self.assertEqual(len(_MAILS), 1)
        tester = api.STORE.get(ts.tester_id("marieke@example.be", "test-salt"))
        self.assertEqual(tester.phone, "+32479123456")
        self.assertEqual(tester.goal, "Walk 30 minutes a day")
        self.assertIn(tester.call_goal, ts.CALL_GOALS)
        self.assertEqual(tester.status, "pending")

    def test_blank_goal_falls_back_to_the_default(self):
        call("POST", "/register", dict(GOOD_REG, goal="   "))
        tester = api.STORE.get(ts.tester_id("marieke@example.be", "test-salt"))
        self.assertEqual(tester.goal, ts.DEFAULT_GOAL)

    def test_registration_can_be_closed(self):
        api.STORE.save_settings({"registration_open": False})
        status, data = call("POST", "/register", GOOD_REG)
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "registration_closed")

    def test_registering_twice_does_not_fork_a_second_record(self):
        register_and_activate()
        status, data = call("POST", "/register", GOOD_REG)
        self.assertEqual(status, 200)
        self.assertTrue(data["already_registered"])
        self.assertEqual(api.STORE.count(), 1)


class TestAuth(unittest.TestCase):
    def setUp(self):
        reset_world()

    def test_verification_link_is_single_use(self):
        call("POST", "/register", GOOD_REG)
        tid = ts.tester_id("marieke@example.be", "test-salt")
        token = api.STORE.issue_link(tid, "verify")
        self.assertEqual(call("POST", "/set-password",
                              {"token": token, "password": "Test1234"})[0], 200)
        status, data = call("POST", "/set-password", {"token": token, "password": "Other123"})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "invalid")

    def test_weak_passwords_are_refused(self):
        call("POST", "/register", GOOD_REG)
        tid = ts.tester_id("marieke@example.be", "test-salt")
        for weak in ("short1A", "alllowercase1", "ALLUPPERCASE1", "NoDigitsHere"):
            token = api.STORE.issue_link(tid, "verify")
            status, data = call("POST", "/set-password", {"token": token, "password": weak})
            self.assertEqual(status, 400, weak)
            self.assertEqual(data["error"], "weak_password")

    def test_login_succeeds_and_wrong_password_counts_down(self):
        register_and_activate()
        self.assertEqual(call("POST", "/login",
                              {"email": "marieke@example.be", "password": "Test1234"})[0], 200)
        status, data = call("POST", "/login",
                            {"email": "marieke@example.be", "password": "nope"})
        self.assertEqual(status, 401)
        self.assertEqual(data["attempts_left"], ts.MAX_FAILED_LOGINS - 1)

    def test_account_locks_after_the_configured_failures(self):
        register_and_activate()
        for _ in range(ts.MAX_FAILED_LOGINS):
            call("POST", "/login", {"email": "marieke@example.be", "password": "nope"})
        status, data = call("POST", "/login",
                            {"email": "marieke@example.be", "password": "Test1234"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"], "account_locked")

    def test_unknown_email_answers_exactly_like_a_wrong_password(self):
        register_and_activate()
        unknown = call("POST", "/login", {"email": "nobody@example.be", "password": "Test1234"})
        self.assertEqual(unknown, (401, {"error": "invalid_credentials"}))

    def test_forgot_password_never_reveals_membership(self):
        register_and_activate()
        known = call("POST", "/forgot", {"email": "marieke@example.be"})
        unknown = call("POST", "/forgot", {"email": "nobody@example.be"})
        self.assertEqual(known, unknown)
        self.assertEqual(known[0], 200)

    def test_setting_a_password_ends_older_sessions(self):
        tid, old = register_and_activate()
        token = api.STORE.issue_link(tid, "reset")
        call("POST", "/set-password", {"token": token, "password": "Newpass1", "mode": "reset"})
        self.assertEqual(call("GET", "/me", token=old)[0], 401)

    def test_protected_routes_refuse_without_a_session(self):
        for method, path in [("GET", "/me"), ("GET", "/chat"), ("POST", "/call"),
                             ("POST", "/feedback"), ("GET", "/call/status")]:
            self.assertEqual(call(method, path, {} if method == "POST" else None)[0], 401,
                             "%s %s" % (method, path))

    def test_expired_session_is_refused(self):
        _, session = register_and_activate()
        key = api.STORE._session_key(ts.token_digest(session))
        rec = api.STORE._get_json(key)
        rec["last_seen_at"] = store.to_iso(
            store.now_dt() - datetime.timedelta(minutes=ts.SESSION_IDLE_MINUTES + 1))
        api.STORE._put_json(key, rec)
        status, data = call("GET", "/me", token=session)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "expired")

    def test_revoked_tester_loses_access_immediately(self):
        tid, session = register_and_activate()
        tester = api.STORE.get(tid)
        tester.status = "revoked"
        api.STORE.put(tester)
        self.assertEqual(call("GET", "/me", token=session)[0], 403)


class TestConsoleSurface(unittest.TestCase):
    def setUp(self):
        reset_world()
        self.tid, self.session = register_and_activate()

    def test_me_never_exposes_the_assigned_call_goal(self):
        status, data = call("GET", "/me", token=self.session)
        self.assertEqual(status, 200)
        self.assertNotIn("call_goal", json.dumps(data))
        self.assertEqual(data["me"]["phone"], "+32479123456")
        self.assertEqual(data["whatsapp"]["join_phrase"], "join olive-tiger")

    def test_chat_seeds_a_greeting_then_answers(self):
        status, data = call("GET", "/chat", token=self.session)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["direction"], "out")

        status, data = call("POST", "/chat", {"text": "Twice only. Rain."}, token=self.session)
        self.assertEqual(status, 200)
        self.assertIn("Twice only", data["reply"])
        self.assertEqual(api.STORE.get(self.tid).track_chat, "in_progress")

    def test_empty_chat_message_is_refused(self):
        self.assertEqual(call("POST", "/chat", {"text": "   "}, token=self.session)[0], 400)

    def test_chat_thread_is_isolated_per_tester(self):
        _, other = register_and_activate(email="joris@example.be")
        call("POST", "/chat", {"text": "mine"}, token=self.session)
        _, data = call("GET", "/chat", token=other)
        self.assertEqual(len(data["messages"]), 1)          # only their own greeting

    def test_ack_is_recorded(self):
        self.assertFalse(call("GET", "/me", token=self.session)[1]["acked"])
        call("POST", "/ack", {}, token=self.session)
        self.assertTrue(call("GET", "/me", token=self.session)[1]["acked"])

    def test_feedback_requires_something_and_stores_a_score(self):
        self.assertEqual(call("POST", "/feedback",
                              {"track": "chat", "text": "", "score": None},
                              token=self.session)[0], 400)
        self.assertEqual(call("POST", "/feedback", {"track": "chat", "score": 8},
                              token=self.session)[0], 200)
        self.assertEqual(api.STORE.get_feedback(self.tid)["chat"]["score"], 8)

    def test_feedback_rejects_a_bad_track_or_score(self):
        self.assertEqual(call("POST", "/feedback", {"track": "sms", "score": 5},
                              token=self.session)[0], 400)
        self.assertEqual(call("POST", "/feedback", {"track": "chat", "score": 11},
                              token=self.session)[0], 400)
        self.assertEqual(call("POST", "/feedback", {"track": "chat", "score": "eight"},
                              token=self.session)[0], 400)

    def test_logout_invalidates_the_session(self):
        call("POST", "/logout", {}, token=self.session)
        self.assertEqual(call("GET", "/me", token=self.session)[0], 401)


class TestCallGates(unittest.TestCase):
    def setUp(self):
        reset_world()
        self.tid, self.session = register_and_activate()
        # Pin the clock outside quiet hours unless a test says otherwise.
        api._is_quiet_now = lambda now=None: False

    def tearDown(self):
        api._is_quiet_now = TestCallGates._real_quiet

    _real_quiet = staticmethod(api._is_quiet_now)

    def test_call_dials_the_registered_number_and_carries_the_goal(self):
        status, data = call("POST", "/call", {}, token=self.session)
        self.assertEqual(status, 200)
        self.assertEqual(data["state"], "dialing")
        cfg = _DIALED[0]["config"]
        self.assertEqual(cfg["to"], "+32479123456")
        self.assertIn(cfg["call_goal"], ts.CALL_GOALS)
        self.assertTrue(cfg["machine_detection"])
        self.assertEqual(cfg["max_seconds"], 300)
        self.assertEqual(_DIALED[0]["token"], "dispatch-tok")

    def test_quiet_hours_refuse_with_an_explanation(self):
        api._is_quiet_now = lambda now=None: True
        status, data = call("POST", "/call", {}, token=self.session)
        self.assertEqual(status, 200)
        self.assertEqual(data["state"], "quiet_hours")
        self.assertTrue(data["reopens_at"])
        self.assertEqual(_DIALED, [], "nothing may be dialled during quiet hours")

    def test_paused_calling_blocks_everyone(self):
        api.STORE.save_settings({"calling_paused": True})
        self.assertEqual(call("POST", "/call", {}, token=self.session)[1]["state"], "paused")
        self.assertEqual(_DIALED, [])

    def test_daily_quota_offers_tomorrow_rather_than_an_error(self):
        api.STORE.save_settings({"daily_call_cap": 1})
        api.STORE.bump_calls_today()
        status, data = call("POST", "/call", {}, token=self.session)
        self.assertEqual(data["state"], "quota_spent")
        self.assertTrue(data["next_slot"])
        self.assertEqual(_DIALED, [])

    def test_spent_five_calls_blocks_a_sixth(self):
        tester = api.STORE.get(self.tid)
        tester.calls_used = tester.calls_max
        api.STORE.put(tester)
        status, data = call("POST", "/call", {}, token=self.session)
        self.assertEqual(data["state"], "calls_spent")
        self.assertEqual(_DIALED, [])

    def test_second_caller_is_queued_not_dialled(self):
        call("POST", "/call", {}, token=self.session)
        _, other = register_and_activate(email="joris@example.be")
        status, data = call("POST", "/call", {}, token=other)
        self.assertEqual(data["state"], "queued")
        self.assertEqual(data["position"], 1)
        self.assertEqual(len(_DIALED), 1, "only one call may be placed at a time")

    def test_leaving_the_queue_frees_the_place(self):
        call("POST", "/call", {}, token=self.session)
        _, other = register_and_activate(email="joris@example.be")
        call("POST", "/call", {}, token=other)
        call("POST", "/call/leave", {}, token=other)
        self.assertEqual(call("GET", "/call/status", token=other)[1]["state"], "idle")

    def test_a_refused_dispatch_releases_the_line(self):
        fake_dispatch(ok=False, skipped="consent-not-granted")
        status, data = call("POST", "/call", {}, token=self.session)
        self.assertEqual(status, 503)
        self.assertEqual(api.STORE.position(self.tid), -1, "a failed dial must not hold the line")


class TestCallLedger(unittest.TestCase):
    """The rule the user set explicitly: only a connected call is deducted."""

    def setUp(self):
        reset_world()
        self.tid, self.session = register_and_activate()
        api._is_quiet_now = lambda now=None: False

    def tearDown(self):
        api._is_quiet_now = TestCallGates._real_quiet

    def _place_and_finish(self, **manifest):
        _, data = call("POST", "/call", {}, token=self.session)
        finish_call(data["call_id"], **manifest)
        return call("GET", "/call/status", token=self.session)[1]

    def test_connected_call_is_deducted_once(self):
        data = self._place_and_finish(answered_by="human", turns=4)
        self.assertEqual(data["outcome"], "connected")
        self.assertEqual(data["calls_used"], 1)
        self.assertEqual(api.STORE.get(self.tid).track_call, "done")

    def test_voicemail_is_not_deducted(self):
        data = self._place_and_finish(answered_by="machine_start", turns=0)
        self.assertEqual(data["outcome"], "voicemail")
        self.assertEqual(data["calls_used"], 0)
        self.assertEqual(data["calls_left"], ts.MAX_CALLS_PER_TESTER)

    def test_no_answer_is_not_deducted(self):
        data = self._place_and_finish(answered_by="", call_status="no-answer", turns=0)
        self.assertEqual(data["outcome"], "no_answer")
        self.assertEqual(data["calls_used"], 0)

    def test_failed_call_is_not_deducted(self):
        data = self._place_and_finish(answered_by="", call_status="failed", turns=0)
        self.assertEqual(data["outcome"], "failed")
        self.assertEqual(data["calls_used"], 0)

    def test_a_finished_call_frees_the_queue_for_the_next_tester(self):
        _, first = call("POST", "/call", {}, token=self.session)
        _, other = register_and_activate(email="joris@example.be")
        call("POST", "/call", {}, token=other)
        finish_call(first["call_id"])
        call("GET", "/call/status", token=self.session)
        self.assertEqual(api.STORE.position(ts.tester_id("joris@example.be", "test-salt")), 0)

    def test_polling_twice_does_not_double_deduct(self):
        _, data = call("POST", "/call", {}, token=self.session)
        finish_call(data["call_id"])
        call("GET", "/call/status", token=self.session)
        call("GET", "/call/status", token=self.session)
        self.assertEqual(api.STORE.get(self.tid).calls_used, 1)

    def test_an_unfinished_call_reports_on_call(self):
        _, data = call("POST", "/call", {}, token=self.session)
        _FAKE_S3.put_object(Bucket=BUCKET, Key="calls/%s/manifest.json" % data["call_id"],
                            Body=json.dumps({"status": "in_progress", "telephony": {}}).encode())
        self.assertEqual(call("GET", "/call/status", token=self.session)[1]["state"], "on_call")


class TestAdmin(unittest.TestCase):
    def setUp(self):
        reset_world()
        self.tid, self.session = register_and_activate()
        self.admin = call("POST", "/admin/login", {"password": "adm1n-secret"})[1]["session"]

    def test_admin_login_requires_the_secret(self):
        self.assertEqual(call("POST", "/admin/login", {"password": "wrong"})[0], 401)

    def test_tester_session_cannot_reach_admin_routes(self):
        self.assertEqual(call("GET", "/admin/testers", token=self.session)[0], 403)

    def test_admin_session_cannot_reach_tester_routes(self):
        self.assertEqual(call("GET", "/me", token=self.admin)[0], 403)

    def test_roster_shows_the_call_goal_but_masks_the_phone(self):
        status, data = call("GET", "/admin/testers", token=self.admin)
        self.assertEqual(status, 200)
        row = data["testers"][0]
        self.assertIn(row["call_goal"], ts.CALL_GOALS)
        self.assertIn("•", row["phone_masked"])
        self.assertNotIn("+32479123456", json.dumps(data))

    def test_overview_reports_the_cohort(self):
        status, data = call("GET", "/admin/overview", token=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(data["kpis"]["registered"], 1)
        self.assertTrue(data["settings"]["registration_open"])

    def test_close_registration_takes_the_form_down(self):
        call("POST", "/admin/settings", {"registration_open": False}, token=self.admin)
        self.assertEqual(call("POST", "/register",
                              dict(GOOD_REG, email="new@example.be"))[0], 403)

    def test_reset_call_limit_gives_the_calls_back(self):
        tester = api.STORE.get(self.tid)
        tester.calls_used = 5
        api.STORE.put(tester)
        call("POST", "/admin/action",
             {"action": "reset_call_limit", "tester_id": self.tid}, token=self.admin)
        self.assertEqual(api.STORE.get(self.tid).calls_used, 0)

    def test_grant_calls_raises_the_ceiling(self):
        call("POST", "/admin/action",
             {"action": "grant_calls", "tester_id": self.tid, "calls": 5}, token=self.admin)
        self.assertEqual(api.STORE.get(self.tid).calls_max, ts.MAX_CALLS_PER_TESTER + 5)

    def test_kill_session_logs_the_tester_out(self):
        call("POST", "/admin/action",
             {"action": "kill_session", "tester_id": self.tid}, token=self.admin)
        self.assertEqual(call("GET", "/me", token=self.session)[0], 401)

    def test_revoke_blocks_login_and_ends_sessions(self):
        call("POST", "/admin/action", {"action": "revoke", "tester_id": self.tid},
             token=self.admin)
        self.assertEqual(call("GET", "/me", token=self.session)[0], 401)
        self.assertEqual(call("POST", "/login",
                              {"email": "marieke@example.be", "password": "Test1234"})[0], 403)

    def test_unlock_clears_a_lockout(self):
        for _ in range(ts.MAX_FAILED_LOGINS):
            call("POST", "/login", {"email": "marieke@example.be", "password": "nope"})
        call("POST", "/admin/action", {"action": "unlock", "tester_id": self.tid},
             token=self.admin)
        self.assertEqual(call("POST", "/login",
                              {"email": "marieke@example.be", "password": "Test1234"})[0], 200)

    def test_resend_verification_issues_a_working_link(self):
        _MAILS.clear()
        status, _ = call("POST", "/admin/action",
                         {"action": "resend_verification", "tester_id": self.tid},
                         token=self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(len(_MAILS), 1)

    def test_call_goal_can_be_changed_but_only_to_a_known_one(self):
        call("POST", "/admin/action",
             {"action": "set_call_goal", "tester_id": self.tid, "call_goal": "REINSTATE_TALK"},
             token=self.admin)
        self.assertEqual(api.STORE.get(self.tid).call_goal, "REINSTATE_TALK")
        self.assertEqual(call("POST", "/admin/action",
                              {"action": "set_call_goal", "tester_id": self.tid,
                               "call_goal": "MAKE_IT_UP"}, token=self.admin)[0], 400)

    def test_unknown_action_and_unknown_tester_are_refused(self):
        self.assertEqual(call("POST", "/admin/action",
                              {"action": "explode", "tester_id": self.tid},
                              token=self.admin)[0], 400)
        self.assertEqual(call("POST", "/admin/action",
                              {"action": "unlock", "tester_id": "tst_nobody"},
                              token=self.admin)[0], 404)


class TestRouting(unittest.TestCase):
    def setUp(self):
        reset_world()

    def test_health_needs_no_credential(self):
        status, data = call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_options_preflight_is_answered(self):
        self.assertEqual(call("OPTIONS", "/register")[0], 200)

    def test_cors_is_pinned_to_the_configured_origin(self):
        headers = api.handler({"requestContext": {"http": {"method": "GET"}},
                               "rawPath": "/health"}, None)["headers"]
        self.assertEqual(headers["Access-Control-Allow-Origin"], api.ALLOW_ORIGIN)

    def test_unknown_path_is_404_not_500(self):
        _, session = register_and_activate()
        self.assertEqual(call("GET", "/nope", token=session)[0], 404)

    def test_oversized_body_is_ignored_rather_than_parsed(self):
        big = {"first_name": "x" * 50000}
        status, _ = call("POST", "/register", big)
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
