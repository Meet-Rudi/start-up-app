"""
meetrudi-tester-call-runner tests — no boto3, no network, synthetic data only.

What these pin down is the behaviour that costs someone something when it breaks: a call that
is counted twice, a promise that is never kept, and a reminder call placed at someone who
already did the thing it was going to remind them about.

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

BUCKET = "meetrudi-ai-data-test"
_FAKE_S3 = FakeS3()

# Only install a boto3 double if a sibling test module hasn't already; replacing theirs would
# hand their subjects a client their assertions never see.
if "boto3" not in sys.modules:
    _boto3 = types.ModuleType("boto3")
    _boto3.client = lambda *a, **k: _FAKE_S3
    sys.modules["boto3"] = _boto3

os.environ["DATA_BUCKET"] = BUCKET
os.environ["PSEUDONYMIZE_SALT"] = "test-salt"
os.environ["TESTER_PBKDF2_ROUNDS"] = "1000"
os.environ["CALL_DISPATCH_URL"] = "https://dispatch.test/"

import store  # noqa: E402
import tester_store as ts  # noqa: E402
import tester_call_runner as runner  # noqa: E402

NOW = store.parse_iso("2026-09-14T10:00:00+00:00")   # 12:00 Brussels — social hours


class _Dispatcher:
    """Captures what the runner asked the dispatcher to dial."""

    def __init__(self):
        self.calls = []
        self.ok = True
        self.reason = "quiet-hours"

    def __call__(self, config):
        self.calls.append(config)
        if self.ok:
            return True, {"ok": True, "call_id": "call_new_1"}
        return False, self.reason


class RunnerTests(unittest.TestCase):
    def setUp(self):
        _FAKE_S3.__init__()
        runner._s3 = _FAKE_S3
        runner.STORE = ts.TesterStore(_FAKE_S3, BUCKET)
        runner.WA = store.ConversationStore(_FAKE_S3, BUCKET)
        runner._cache.clear()
        self.dispatch = _Dispatcher()
        runner._dispatch = self.dispatch
        runner.gateway = types.SimpleNamespace(has_headroom=lambda: True)

    # ---------------------------------------------------------------- fixtures
    def _tester(self, tid="tst_a", **kw):
        base = dict(tester_id=tid, first_name="Marieke", phone="+32479123456",
                    locale="en", consent_health=True, status="active",
                    goal="Move more", wa_user_id="wa_marieke")
        base.update(kw)
        t = ts.Tester(**base)
        runner.STORE.put(t)
        return t

    def _finished_call(self, call_id="call_1", tid="tst_a", call_goal="GET_TO_KNOW",
                       turns=6, answered_by="human", goal_domain="fitness",
                       check_in_minutes=None, ended="2026-09-14T09:00:00+00:00"):
        manifest = {
            "call_id": call_id, "status": "completed", "ended_at": ended,
            "totals": {"turns": turns},
            "telephony": {"answered_by": answered_by, "call_status": "completed"},
            "config": {"call_goal": call_goal, "tester_id": tid},
            "outcome": {"goal": "Move more", "goal_domain": goal_domain,
                        "check_in_minutes": check_in_minutes,
                        "check_in_about": "how the ride went" if check_in_minutes else None},
        }
        _FAKE_S3.put_object(Bucket=BUCKET, Key="calls/%s/manifest.json" % call_id,
                            Body=json.dumps(manifest).encode())
        _FAKE_S3.put_object(Bucket=BUCKET, Key="calls/_index/2026-09-14/%s.json" % call_id,
                            Body=json.dumps({"call_id": call_id, "tester_id": tid,
                                             "status": "completed"}).encode())
        return manifest

    # ---------------------------------------------------------------- the ledger
    def test_a_connected_call_is_counted_whoever_placed_it(self):
        """The console only counted calls its own browser polled, so Rudi-initiated calls were
        invisible to the ledger."""
        self._tester()
        self._finished_call()
        runner.reconcile(NOW)
        t = runner.STORE.get("tst_a")
        self.assertEqual(t.calls_used, 1)
        self.assertEqual(t.last_call_outcome, "connected")
        self.assertEqual(t.track_call, "done")

    def test_voicemail_and_no_answer_are_not_counted(self):
        self._tester()
        self._finished_call(call_id="c_vm", answered_by="machine_start", turns=0)
        self._finished_call(call_id="c_na", answered_by="", turns=0)
        runner.reconcile(NOW)
        self.assertEqual(runner.STORE.get("tst_a").calls_used, 0)

    def test_our_own_outage_is_not_charged_to_the_tester(self):
        """A call abandoned with ai-unavailable is marked completed and carries a turn, so the
        turn count alone would score it as a conversation and bill them for our outage."""
        self._tester()
        manifest = self._finished_call(call_id="c_dead", turns=1, goal_domain=None)
        manifest["end_reason"] = "ai-unavailable"
        manifest["telephony"]["error"] = "ai-unavailable"
        _FAKE_S3.put_object(Bucket=BUCKET, Key="calls/c_dead/manifest.json",
                            Body=json.dumps(manifest).encode())
        runner.reconcile(NOW)
        t = runner.STORE.get("tst_a")
        self.assertEqual(t.calls_used, 0)
        self.assertEqual(t.last_call_outcome, "failed")

    def test_reconciling_twice_does_not_double_count(self):
        self._tester()
        self._finished_call()
        runner.reconcile(NOW)
        runner.reconcile(NOW)
        self.assertEqual(runner.STORE.get("tst_a").calls_used, 1)

    # ---------------------------------------------------------------- follow-ups earned
    def test_a_promise_on_a_call_schedules_the_redial(self):
        self._tester()
        self._finished_call(call_goal="GOAL_FOLLOWUP", check_in_minutes=90, goal_domain=None)
        runner.reconcile(NOW)
        entries = runner.STORE.scheduled_calls()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["reason"], "promise")
        self.assertEqual(entries[0]["note"], "how the ride went")

    def test_get_to_know_with_a_domain_schedules_the_whatsapp_reminder(self):
        self._tester()
        self._finished_call(call_goal="GET_TO_KNOW", goal_domain="fitness")
        runner.reconcile(NOW)
        entries = runner.STORE.scheduled_calls()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["reason"], "whatsapp_reminder")
        gap = (store.parse_iso(entries[0]["at"])
               - store.parse_iso("2026-09-14T09:00:00+00:00")).total_seconds() / 3600
        self.assertAlmostEqual(gap, 22.0, places=2)

    def test_get_to_know_without_a_domain_schedules_nothing(self):
        """No focus area means the call didn't achieve what it was for — there is nothing to
        hand over to WhatsApp, so ringing them again would just be noise."""
        self._tester()
        self._finished_call(call_goal="GET_TO_KNOW", goal_domain=None)
        runner.reconcile(NOW)
        self.assertEqual(runner.STORE.scheduled_calls(), [])

    # ---------------------------------------------------------------- placing the calls
    def test_a_due_call_is_placed_with_the_locked_number(self):
        self._tester()
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise", note="the bike ride")
        placed, _ = runner.place_due(NOW)
        self.assertEqual(placed, 1)
        cfg = self.dispatch.calls[0]
        self.assertEqual(cfg["to"], "+32479123456")
        self.assertEqual(cfg["call_goal"], "GOAL_FOLLOWUP")
        self.assertIn("the bike ride", cfg["notes"])
        self.assertNotIn("speak_only", cfg)
        self.assertEqual(runner.STORE.scheduled_calls(), [], "a placed call leaves the queue")

    def test_a_call_back_tells_rudi_when_the_promise_was_made(self):
        """A promise read out a day later has to arrive with its day, not as a bare "7PM"."""
        tester = self._tester()
        entry = {"reason": "promise", "note": "how the 7PM walk went",
                 "created_at": "2026-09-14T13:00:00+00:00"}
        cfg = runner._config_for(entry, tester, now=store.parse_iso("2026-09-15T12:00:00+00:00"))
        self.assertIn("You promised yesterday at 15:00", cfg["notes"])
        self.assertIn("how the 7PM walk went", cfg["notes"])

    def test_the_reminder_is_cancelled_if_they_already_messaged(self):
        t = self._tester()
        runner.WA.put_meta(store.ContactMeta(user_id=t.wa_user_id, phone=t.phone,
                                             last_inbound_at="2026-09-14T09:30:00+00:00"))
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "whatsapp_reminder",
                                   since="2026-09-14T09:00:00+00:00")
        placed, skipped = runner.place_due(NOW)
        self.assertEqual((placed, skipped), (0, 1))
        self.assertEqual(self.dispatch.calls, [], "nobody should be rung about a thing they did")
        self.assertEqual(runner.STORE.scheduled_calls(), [])

    def test_a_frozen_whatsapp_thread_stops_every_pending_call(self):
        """If the objection gate decided we may have the wrong person, phoning them is the same
        mistake in a louder channel. Every pending call goes, not just the one that came due."""
        t = self._tester()
        runner.WA.put_meta(store.ContactMeta(user_id=t.wa_user_id, phone=t.phone,
                                             status="frozen", frozen_kind="wrong_number"))
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise", note="the bike ride")
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "whatsapp_reminder", note="fitness")
        placed, skipped = runner.place_due(NOW)
        self.assertEqual(placed, 0)
        self.assertEqual(self.dispatch.calls, [])
        self.assertEqual(runner.STORE.scheduled_calls(), [], "both reasons must be dropped")

    def test_an_unreadable_contact_holds_the_call_rather_than_cancelling_it(self):
        """Fail closed, but only as a HOLD. Cancelling on a transient read error would silently
        delete work nobody decided to drop."""
        t = self._tester()
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise", note="the bike ride")

        class _Boom:
            def get_meta(self, uid):
                raise RuntimeError("s3 unavailable")
        real_wa, runner.WA = runner.WA, _Boom()
        try:
            placed, skipped = runner.place_due(NOW)
        finally:
            runner.WA = real_wa
        self.assertEqual((placed, skipped), (0, 1))
        self.assertEqual(self.dispatch.calls, [], "never dial while the freeze state is unknown")
        self.assertEqual(len(runner.STORE.scheduled_calls()), 1, "still queued for the next tick")

    def test_a_placed_call_is_remembered_on_the_tester(self):
        """The console works out the next call's number from this, before reconcile has run."""
        self._tester()
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise", note="the bike ride")
        placed, _ = runner.place_due(NOW)
        self.assertEqual(placed, 1)
        t = runner.STORE.get("tst_a")
        self.assertEqual(t.last_call_id, "call_new_1")
        self.assertEqual(t.call_goal, "GOAL_FOLLOWUP")

    def test_the_reminder_is_placed_if_they_stayed_silent(self):
        t = self._tester()
        runner.WA.put_meta(store.ContactMeta(user_id=t.wa_user_id, phone=t.phone,
                                             last_inbound_at="2026-09-14T08:00:00+00:00"))
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "whatsapp_reminder",
                                   note="fitness", since="2026-09-14T09:00:00+00:00")
        placed, _ = runner.place_due(NOW)
        self.assertEqual(placed, 1)
        self.assertIn("WhatsApp", self.dispatch.calls[0]["notes"])

    def test_without_ai_headroom_it_speaks_one_line_instead(self):
        """Ringing someone and dying on turn one is worse than one clear sentence."""
        runner.gateway = types.SimpleNamespace(has_headroom=lambda: False)
        self._tester()
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "whatsapp_reminder",
                                   since="2026-09-14T09:00:00+00:00")
        placed, _ = runner.place_due(NOW)
        self.assertEqual(placed, 1)
        cfg = self.dispatch.calls[0]
        self.assertIn("WhatsApp", cfg["speak_only"])
        self.assertTrue(cfg["speak_only"].strip())

    def test_a_tester_with_no_calls_left_is_not_rung(self):
        self._tester(calls_used=5, calls_max=5)
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise")
        placed, skipped = runner.place_due(NOW)
        self.assertEqual((placed, skipped), (0, 1))
        self.assertEqual(self.dispatch.calls, [])

    def test_paused_calling_holds_everything(self):
        self._tester()
        runner.STORE.save_settings({"calling_paused": True})
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise")
        placed, _ = runner.place_due(NOW)
        self.assertEqual(placed, 0)
        self.assertEqual(self.dispatch.calls, [])
        self.assertEqual(len(runner.STORE.scheduled_calls()), 1, "held, not dropped")

    def test_quiet_hours_move_the_call_rather_than_dropping_it(self):
        self._tester()
        self.dispatch.ok, self.dispatch.reason = False, "quiet-hours"
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise", note="the ride")
        runner.place_due(NOW)
        entries = runner.STORE.scheduled_calls()
        self.assertEqual(len(entries), 1, "a quiet-hours refusal must reschedule, not discard")
        self.assertEqual(entries[0]["note"], "the ride")

    def test_a_revoked_tester_is_never_rung(self):
        self._tester(status="revoked")
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise")
        placed, _ = runner.place_due(NOW)
        self.assertEqual(placed, 0)
        self.assertEqual(self.dispatch.calls, [])

    def test_nothing_due_is_a_quiet_no_op(self):
        self._tester()
        self.assertEqual(runner.place_due(NOW), (0, 0))

    # ---------------------------------------------------------------- one reach-back, ever
    def test_placing_the_follow_up_spends_the_one_allowance(self):
        self._tester()
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise")
        runner.place_due(NOW)
        self.assertTrue(runner.STORE.get("tst_a").followup_call_at)

    def test_a_follow_up_call_never_earns_another_follow_up(self):
        """Otherwise each successful call schedules the next one and Rudi rings forever."""
        self._tester(followup_call_at="2026-09-14T08:00:00+00:00")
        self._finished_call(call_goal="GOAL_FOLLOWUP", check_in_minutes=60, goal_domain=None)
        runner.reconcile(NOW)
        self.assertEqual(runner.STORE.scheduled_calls(), [])

    def test_an_unanswered_follow_up_does_not_buy_another(self):
        self._tester()
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "whatsapp_reminder",
                                   since="2026-09-14T09:00:00+00:00")
        runner.place_due(NOW)
        spent = runner.STORE.get("tst_a").followup_call_at
        self.assertTrue(spent)
        self._finished_call(call_id="c_noans", answered_by="", turns=0, goal_domain="fitness")
        runner.reconcile(NOW)
        self.assertEqual(runner.STORE.scheduled_calls(), [])
        self.assertEqual(runner.STORE.get("tst_a").followup_call_at, spent,
                         "an unanswered reach-back is still the reach-back")

    def test_a_refused_dispatch_is_dropped_not_retried_every_tick(self):
        self._tester()
        self.dispatch.ok, self.dispatch.reason = False, "consent-not-granted"
        runner.STORE.schedule_call("tst_a", store.to_iso(NOW), "promise")
        runner.place_due(NOW)
        self.assertEqual(runner.STORE.scheduled_calls(), [],
                         "only quiet hours is a 'not yet'; a refusal must not re-attempt forever")

    def test_writing_on_whatsapp_returns_the_reach_back(self):
        t = self._tester(followup_call_at="2026-09-14T08:00:00+00:00")
        runner.WA.put_meta(store.ContactMeta(user_id=t.wa_user_id, phone=t.phone,
                                             last_inbound_at="2026-09-14T09:00:00+00:00"))
        runner.restore_followups()
        self.assertEqual(runner.STORE.get("tst_a").followup_call_at, "")

    def test_continued_silence_does_not_return_it(self):
        t = self._tester(followup_call_at="2026-09-14T08:00:00+00:00")
        runner.WA.put_meta(store.ContactMeta(user_id=t.wa_user_id, phone=t.phone,
                                             last_inbound_at="2026-09-14T07:00:00+00:00"))
        runner.restore_followups()
        self.assertTrue(runner.STORE.get("tst_a").followup_call_at,
                        "going quiet is exactly when Rudi must stop calling")


if __name__ == "__main__":
    unittest.main()
