"""
R1–R3 tests: quiet-hours gate, social-hours helpers, and the anti-drift proactive scheduler.
Zero external deps. All times are constructed in UTC; Europe/Brussels is UTC+1 on 2026-01-15
(CET, no DST), so local = UTC + 1h throughout these fixtures.

Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import os
import sys
import datetime
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

import store  # noqa: E402
from store import (ConversationStore, ContactMeta, Message, user_id,  # noqa: E402
                   is_quiet, last_social_before, next_social_start,
                   compute_next_proactive, _tz, window_open_until)
from fake_s3 import FakeS3  # noqa: E402

TZ = _tz("Europe/Brussels")
UTC = datetime.timezone.utc


def U(y, mo, d, h, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=UTC)


def iso(dt):
    return store.to_iso(dt)


class QuietHoursTests(unittest.TestCase):
    def test_is_quiet_boundaries(self):
        self.assertTrue(is_quiet(U(2026, 1, 15, 21, 0), TZ))    # local 22:00 → quiet
        self.assertFalse(is_quiet(U(2026, 1, 15, 11, 0), TZ))   # local 12:00 → social
        self.assertTrue(is_quiet(U(2026, 1, 15, 2, 0), TZ))     # local 03:00 → quiet
        self.assertTrue(is_quiet(U(2026, 1, 15, 5, 29), TZ))    # local 06:29 → quiet
        self.assertFalse(is_quiet(U(2026, 1, 15, 5, 31), TZ))   # local 06:31 → social

    def test_last_social_before_pulls_to_pre_quiet(self):
        # local 03:00 (quiet) → previous evening 21:25 local = 20:25 UTC
        got = last_social_before(U(2026, 1, 15, 2, 0), TZ)
        self.assertEqual(got, U(2026, 1, 14, 20, 25))

    def test_last_social_before_social_is_identity(self):
        dt = U(2026, 1, 15, 11, 0)
        self.assertEqual(last_social_before(dt, TZ), dt)

    def test_next_social_start_from_quiet(self):
        # local 03:00 (quiet) → same-day 06:30 local = 05:30 UTC
        self.assertEqual(next_social_start(U(2026, 1, 15, 2, 0), TZ), U(2026, 1, 15, 5, 30))

    def test_next_social_start_evening_rolls_to_morning(self):
        # local 22:00 (quiet) → next day 06:30 local = 05:30 UTC
        self.assertEqual(next_social_start(U(2026, 1, 15, 21, 0), TZ), U(2026, 1, 16, 5, 30))


def _meta(**kw):
    base = dict(user_id="wa_x", phone="+320000000000", consent_state="granted",
                status="active", timezone="Europe/Brussels")
    base.update(kw)
    return ContactMeta(**base)


class SchedulerTests(unittest.TestCase):
    def test_no_schedule_without_consent(self):
        m = _meta(consent_state="unknown", window_open_until=iso(U(2026, 1, 16, 11, 0)))
        self.assertEqual(compute_next_proactive(m, U(2026, 1, 15, 11, 0)), ("", ""))

    def test_no_schedule_when_never_messaged(self):
        self.assertEqual(compute_next_proactive(_meta(), U(2026, 1, 15, 11, 0)), ("", ""))

    def test_nudge_for_social_hours_expiry(self):
        # last inbound local 12:00 → window closes next day local 12:00 (social).
        # nudge target = expiry - 2h = next day 10:00 local = 09:00 UTC (social) → nudge there.
        m = _meta(window_open_until=iso(U(2026, 1, 16, 11, 0)))
        at, kind = compute_next_proactive(m, U(2026, 1, 15, 11, 0))
        self.assertEqual(kind, "nudge")
        self.assertEqual(store.parse_iso(at), U(2026, 1, 16, 9, 0))

    def test_nudge_pulled_forward_when_expiry_in_quiet(self):
        # last inbound local 05:00 → window closes next day local 05:00 (QUIET, 3 a.m.-ish).
        # Anti-drift: nudge in the *previous* social window, 21:25 local = 20:25 UTC.
        m = _meta(window_open_until=iso(U(2026, 1, 16, 4, 0)))
        at, kind = compute_next_proactive(m, U(2026, 1, 15, 4, 0))
        self.assertEqual(kind, "nudge")
        self.assertEqual(store.parse_iso(at), U(2026, 1, 15, 20, 25))
        self.assertFalse(is_quiet(store.parse_iso(at), TZ))   # never scheduled into quiet hours

    def test_after_nudge_falls_back_to_template(self):
        wou = iso(U(2026, 1, 16, 4, 0))
        m = _meta(window_open_until=wou, nudge_sent_for_window=wou)  # nudge already spent
        at, kind = compute_next_proactive(m, U(2026, 1, 15, 4, 0))
        self.assertEqual(kind, "template")
        # window closes local 05:00 quiet → first social slot 06:30 local = 05:30 UTC
        self.assertEqual(store.parse_iso(at), U(2026, 1, 16, 5, 30))

    def test_template_cadence_gap_enforced(self):
        wou = iso(U(2026, 1, 10, 11, 0))                       # window long closed
        m = _meta(window_open_until=wou, nudge_sent_for_window=wou,
                  reengage_count=1, last_reengage_at=iso(U(2026, 1, 15, 8, 0)))
        at, kind = compute_next_proactive(m, U(2026, 1, 15, 9, 0))
        self.assertEqual(kind, "template")
        # next template must be ≥ last_reengage + 48h (2026-01-17 08:00 UTC = 09:00 local, social)
        self.assertGreaterEqual(store.parse_iso(at), U(2026, 1, 17, 8, 0))

    def test_dormant_after_max_misses(self):
        wou = iso(U(2026, 1, 10, 11, 0))
        m = _meta(window_open_until=wou, nudge_sent_for_window=wou,
                  reengage_count=store.MAX_TEMPLATE_MISSES)
        self.assertEqual(compute_next_proactive(m, U(2026, 1, 15, 11, 0)), ("", ""))

    def test_keep_warm_off_schedules_nothing(self):
        m = _meta(window_open_until=iso(U(2026, 1, 16, 11, 0)), keep_warm=False)
        self.assertEqual(compute_next_proactive(m, U(2026, 1, 15, 11, 0)), ("", ""))

    def test_test_mode_nudges_shortly_after_last_inbound(self):
        saved = (store.TEST_MODE, store.TEST_LEAD)
        store.TEST_MODE, store.TEST_LEAD = True, datetime.timedelta(minutes=2)
        try:
            m = _meta(window_open_until=iso(U(2026, 1, 15, 12, 0)),
                      last_inbound_at=iso(U(2026, 1, 15, 11, 0)))
            at, kind = compute_next_proactive(m, U(2026, 1, 15, 11, 0))
            self.assertEqual(kind, "nudge")
            self.assertEqual(store.parse_iso(at), U(2026, 1, 15, 11, 2))   # last_inbound + 2 min
        finally:
            store.TEST_MODE, store.TEST_LEAD = saved


class StoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.store = ConversationStore(self.s3, "b")
        self.uid = user_id("+320000000000", "salt")
        # consenting Belgian contact
        self.store.put_meta(ContactMeta(user_id=self.uid, phone="+320000000000",
                                        consent_state="granted", timezone="Europe/Brussels"))

    def test_record_inbound_schedules_a_nudge(self):
        self.store.record_inbound(self.uid, "+320000000000",
                                  Message(id="A", direction="in", text="hi", at=iso(U(2026, 1, 15, 11, 0))))
        m = self.store.get_meta(self.uid)
        self.assertEqual(m.next_proactive_kind, "nudge")
        self.assertTrue(m.next_proactive_at)
        self.assertEqual(m.nudge_sent_for_window, "")   # fresh window, nudge not yet spent

    def test_proactive_nudge_send_flips_to_template(self):
        self.store.record_inbound(self.uid, "+320000000000",
                                  Message(id="A", direction="in", text="hi", at=iso(U(2026, 1, 15, 11, 0))))
        wou = self.store.get_meta(self.uid).window_open_until
        # runner sends the nudge (proactive) at the scheduled time
        self.store.record_outbound(self.uid,
                                   Message(id="N", direction="out", text="still there? 🙂", at=iso(U(2026, 1, 16, 9, 0))),
                                   proactive_kind="nudge")
        m = self.store.get_meta(self.uid)
        self.assertEqual(m.nudge_sent_for_window, wou)
        self.assertEqual(m.next_proactive_kind, "template")   # nudge spent → template fallback next

    def test_reactive_reply_does_not_spend_nudge(self):
        self.store.record_inbound(self.uid, "+320000000000",
                                  Message(id="A", direction="in", text="hi", at=iso(U(2026, 1, 15, 11, 0))))
        self.store.record_outbound(self.uid,
                                   Message(id="R", direction="out", text="hey!", at=iso(U(2026, 1, 15, 11, 5)),
                                           operator_id="op"))   # no proactive_kind → reactive
        m = self.store.get_meta(self.uid)
        self.assertEqual(m.nudge_sent_for_window, "")          # nudge still available
        self.assertEqual(m.next_proactive_kind, "nudge")

    def test_list_due_picks_scheduled(self):
        self.store.record_inbound(self.uid, "+320000000000",
                                  Message(id="A", direction="in", text="hi", at=iso(U(2026, 1, 15, 11, 0))))
        self.assertEqual(self.store.list_due(U(2026, 1, 14, 0, 0)), [])       # before scheduled time
        due = self.store.list_due(U(2026, 1, 17, 0, 0))                       # well after
        self.assertEqual([m.user_id for m in due], [self.uid])

    def test_set_keep_warm_toggles_and_reschedules(self):
        self.store.record_inbound(self.uid, "+320000000000",
                                  Message(id="A", direction="in", text="hi", at=iso(U(2026, 1, 15, 11, 0))))
        self.assertTrue(self.store.get_meta(self.uid).next_proactive_kind)   # scheduled
        self.store.set_keep_warm(self.uid, False, now=U(2026, 1, 15, 11, 30))
        off = self.store.get_meta(self.uid)
        self.assertFalse(off.keep_warm)
        self.assertEqual(off.next_proactive_kind, "")                        # cleared
        self.store.set_keep_warm(self.uid, True, now=U(2026, 1, 15, 11, 30))
        self.assertTrue(self.store.get_meta(self.uid).next_proactive_kind)   # rescheduled (window still open)


if __name__ == "__main__":
    unittest.main()
