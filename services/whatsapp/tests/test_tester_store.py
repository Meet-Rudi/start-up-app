"""
TesterStore contract tests — no boto3, no network, synthetic data only (CLAUDE.md §8).

These cover the parts where a bug is a security or a product failure rather than a cosmetic one:
single-use links, idle session expiry, the call queue, and the five-call ledger.

Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import os
import sys
import types
import datetime
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

from fake_s3 import FakeS3  # noqa: E402

# Keep PBKDF2 cheap in tests. The production value is set in the SAM template, not here.
os.environ["TESTER_PBKDF2_ROUNDS"] = "1000"
os.environ.setdefault("DATA_BUCKET", "meetrudi-ai-data-test")

sys.modules.setdefault("boto3", types.ModuleType("boto3"))
sys.modules["boto3"].client = lambda *a, **k: FakeS3()

import store  # noqa: E402
import tester_store as ts  # noqa: E402

BUCKET = "meetrudi-ai-data-test"
SALT = "test-salt"


def make_store():
    return ts.TesterStore(FakeS3(), BUCKET)


def make_tester(store_, email="marieke@example.be", **kw):
    tid = ts.tester_id(email, SALT)
    t = ts.Tester(tester_id=tid, email=email, first_name="Marieke", last_name="D'Hondt",
                  phone="+32479123456", **kw)
    return store_.put(t)


class TestIdentity(unittest.TestCase):
    def test_id_is_deterministic_and_carries_no_pii(self):
        a = ts.tester_id("Marieke@Example.BE", SALT)
        b = ts.tester_id("marieke@example.be", SALT)
        self.assertEqual(a, b, "same address, different case, must be one record")
        self.assertTrue(a.startswith("tst_"))
        self.assertNotIn("marieke", a.lower())
        self.assertNotEqual(a, ts.tester_id("joris@example.be", SALT))

    def test_id_depends_on_the_salt(self):
        self.assertNotEqual(ts.tester_id("a@b.be", "salt-one"),
                            ts.tester_id("a@b.be", "salt-two"))

    def test_password_round_trip(self):
        encoded = ts.hash_password("Test1234")
        self.assertTrue(ts.verify_password("Test1234", encoded))
        self.assertFalse(ts.verify_password("test1234", encoded))
        self.assertNotIn("Test1234", encoded)

    def test_verify_password_survives_garbage(self):
        for junk in ("", "not-a-hash", "pbkdf2_sha256$oops", None):
            self.assertFalse(ts.verify_password("Test1234", junk))

    def test_call_goals_round_robin_over_all_four(self):
        assigned = [ts.call_goal_for(i) for i in range(8)]
        self.assertEqual(set(assigned), set(ts.CALL_GOALS))
        self.assertEqual(assigned[0], assigned[4])

    def test_mask_phone_keeps_it_unusable(self):
        self.assertEqual(ts.mask_phone("+32479123456"), "+32 479 ••• •56")
        self.assertNotIn("1234", ts.mask_phone("+32479123456"))
        self.assertEqual(ts.mask_phone(""), "")
        self.assertEqual(ts.mask_phone("+3247"), "", "too short to mask meaningfully")


class TestRecord(unittest.TestCase):
    def test_public_hides_the_call_goal_and_every_credential(self):
        t = ts.Tester(tester_id="tst_x", email="a@b.be", phone="+32479123456",
                      call_goal="GOAL_FOLLOWUP", password_hash="pbkdf2_sha256$1$x$y")
        pub = t.public()
        self.assertNotIn("call_goal", pub)
        self.assertNotIn("password_hash", pub)
        self.assertNotIn("email", pub)

    def test_admin_row_masks_the_phone(self):
        t = ts.Tester(tester_id="tst_x", phone="+32479123456")
        row = t.admin_row()
        self.assertNotIn("phone", row)
        self.assertIn("•", row["phone_masked"])

    def test_round_trip_through_s3(self):
        st = make_store()
        make_tester(st, goal="Walk 30 minutes a day")
        got = st.get(ts.tester_id("marieke@example.be", SALT))
        self.assertEqual(got.goal, "Walk 30 minutes a day")
        self.assertEqual(got.phone, "+32479123456")

    def test_roster_lists_every_profile(self):
        st = make_store()
        for i in range(3):
            make_tester(st, email="t%d@example.be" % i)
        self.assertEqual(len(st.list_all()), 3)
        self.assertEqual(st.count(), 3)


class TestLinks(unittest.TestCase):
    def test_link_is_single_use(self):
        st = make_store()
        token = st.issue_link("tst_a", "verify")
        self.assertEqual(st.consume_link(token, "verify"), ("tst_a", ""))
        # Replaying it must find nothing at all, not merely a "used" flag.
        self.assertEqual(st.consume_link(token, "verify"), (None, "invalid"))

    def test_link_kind_is_enforced(self):
        st = make_store()
        token = st.issue_link("tst_a", "verify")
        self.assertEqual(st.consume_link(token, "reset"), (None, "invalid"))

    def test_expired_link_is_refused_and_burnt(self):
        st = make_store()
        token = st.issue_link("tst_a", "verify", ttl_hours=-1)
        self.assertEqual(st.consume_link(token, "verify"), (None, "expired"))
        self.assertEqual(st.consume_link(token, "verify"), (None, "invalid"))

    def test_revoke_links_drops_only_that_testers_links(self):
        st = make_store()
        mine = st.issue_link("tst_a", "verify")
        theirs = st.issue_link("tst_b", "verify")
        self.assertEqual(st.revoke_links("tst_a"), 1)
        self.assertEqual(st.consume_link(mine, "verify"), (None, "invalid"))
        self.assertEqual(st.consume_link(theirs, "verify"), ("tst_b", ""))

    def test_unknown_token_is_invalid(self):
        self.assertEqual(make_store().consume_link("made-up", "verify"), (None, "invalid"))


class TestSessions(unittest.TestCase):
    def test_session_round_trip(self):
        st = make_store()
        token = st.open_session("tst_a")
        sess, reason = st.touch_session(token)
        self.assertEqual(reason, "")
        self.assertEqual(sess["tester_id"], "tst_a")

    def test_idle_session_expires_and_is_removed(self):
        st = make_store()
        token = st.open_session("tst_a")
        stale = store.to_iso(store.now_dt()
                             - datetime.timedelta(minutes=ts.SESSION_IDLE_MINUTES + 1))
        key = st._session_key(ts.token_digest(token))
        rec = st._get_json(key)
        rec["last_seen_at"] = stale
        st._put_json(key, rec)

        self.assertEqual(st.touch_session(token), (None, "expired"))
        self.assertEqual(st.touch_session(token), (None, "unauthorized"))

    def test_touch_slides_the_window(self):
        st = make_store()
        token = st.open_session("tst_a")
        key = st._session_key(ts.token_digest(token))
        nearly = store.to_iso(store.now_dt()
                              - datetime.timedelta(minutes=ts.SESSION_IDLE_MINUTES - 1))
        rec = st._get_json(key)
        rec["last_seen_at"] = nearly
        st._put_json(key, rec)

        self.assertEqual(st.touch_session(token)[1], "")
        self.assertGreater(st._get_json(key)["last_seen_at"], nearly)

    def test_kill_sessions_drops_all_of_one_testers(self):
        st = make_store()
        a1, a2 = st.open_session("tst_a"), st.open_session("tst_a")
        b1 = st.open_session("tst_b")
        self.assertEqual(st.kill_sessions("tst_a"), 2)
        self.assertEqual(st.touch_session(a1), (None, "unauthorized"))
        self.assertEqual(st.touch_session(a2), (None, "unauthorized"))
        self.assertEqual(st.touch_session(b1)[1], "")

    def test_ack_is_recorded_per_session(self):
        st = make_store()
        token = st.open_session("tst_a")
        self.assertEqual(st.touch_session(token)[0].get("acked_at"), "")
        st.ack_session(token)
        self.assertTrue(st.touch_session(token)[0]["acked_at"])


class TestQueue(unittest.TestCase):
    def test_first_caller_goes_straight_on_the_line(self):
        st = make_store()
        st.enqueue("tst_a")
        self.assertEqual(st.position("tst_a"), 0)

    def test_second_and_third_are_queued_in_order(self):
        st = make_store()
        for tid in ("tst_a", "tst_b", "tst_c"):
            st.enqueue(tid)
        self.assertEqual([st.position(t) for t in ("tst_a", "tst_b", "tst_c")], [0, 1, 2])

    def test_enqueue_is_idempotent(self):
        st = make_store()
        st.enqueue("tst_a")
        st.enqueue("tst_b")
        st.enqueue("tst_b")            # asking twice must not add a second place
        self.assertEqual(st.position("tst_b"), 1)
        self.assertEqual(len(st.queue()["waiting"]), 1)

    def test_dequeue_promotes_the_next_in_line(self):
        st = make_store()
        for tid in ("tst_a", "tst_b", "tst_c"):
            st.enqueue(tid)
        st.dequeue("tst_a")
        self.assertEqual(st.position("tst_b"), 0)
        self.assertEqual(st.position("tst_c"), 1)
        self.assertEqual(st.position("tst_a"), -1)

    def test_leaving_from_the_middle_keeps_the_active_call(self):
        st = make_store()
        for tid in ("tst_a", "tst_b", "tst_c"):
            st.enqueue(tid)
        st.dequeue("tst_b")
        self.assertEqual(st.position("tst_a"), 0)
        self.assertEqual(st.position("tst_c"), 1)

    def test_call_id_is_attached_to_the_active_slot(self):
        st = make_store()
        st.enqueue("tst_a")
        st.set_active_call("tst_a", "call_123")
        self.assertEqual(st.queue()["active"]["call_id"], "call_123")


class TestFeedbackAndQuota(unittest.TestCase):
    def test_feedback_keeps_earlier_answers(self):
        st = make_store()
        st.save_feedback("tst_a", "chat", 7, "first thought")
        rec = st.save_feedback("tst_a", "chat", 9, "second thought")
        self.assertEqual(rec["score"], 9)
        self.assertEqual(len(rec["history"]), 1)
        self.assertEqual(rec["history"][0]["text"], "first thought")

    def test_get_feedback_returns_one_entry_per_track(self):
        st = make_store()
        st.save_feedback("tst_a", "chat", 7, "x")
        st.save_feedback("tst_a", "call", None, "y")
        got = st.get_feedback("tst_a")
        self.assertEqual(sorted(got), ["call", "chat"])

    def test_daily_counter_increments(self):
        st = make_store()
        self.assertEqual(st.calls_today(), 0)
        st.bump_calls_today()
        st.bump_calls_today()
        self.assertEqual(st.calls_today(), 2)

    def test_settings_have_safe_defaults(self):
        s = make_store().settings()
        self.assertTrue(s["registration_open"])
        self.assertFalse(s["calling_paused"])

    def test_settings_merge_rather_than_replace(self):
        st = make_store()
        st.save_settings({"registration_open": False})
        st.save_settings({"calling_paused": True})
        s = st.settings()
        self.assertFalse(s["registration_open"])
        self.assertTrue(s["calling_paused"])


if __name__ == "__main__":
    unittest.main()
