"""
Outreach limits — Rudi may not chase somebody who has stopped answering.

The reported case: Rudi replied, a proactive nudge landed three minutes later, then another, with
no word from the person in between. Two rules now bind every system-initiated WhatsApp send,
inside the 24h window and outside it:

  * at most two reach-outs in a row without the person writing back;
  * at least 45 minutes after the last message we sent them since they last wrote — Rudi's own
    reply included, which is what stops a nudge chasing his answer within minutes.

Reactive replies are never held (the person just wrote to us) and never count towards the cap.

Times are UTC in September 2026; Brussels is UTC+2, so 08:00-19:00Z is all social hours.

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
from fake_s3 import FakeS3  # noqa: E402

UID = "wa_limits"
PHONE = "+320000000001"


def T(hhmm, day=13):
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.datetime(2026, 9, day, h, m, tzinfo=datetime.timezone.utc)


def iso(dt):
    return store.to_iso(dt)


class OutreachLimits(unittest.TestCase):
    def setUp(self):
        self.st = store.ConversationStore(FakeS3(), "b")
        self.st.put_meta(store.ContactMeta(user_id=UID, phone=PHONE, consent_state="granted",
                                           keep_warm=True, locale="en",
                                           timezone="Europe/Brussels"))

    # ------------------------------------------------------------------ helpers
    def _in(self, at, text="hi"):
        return self.st.record_inbound(UID, PHONE, store.Message(
            id="in-%s" % at.strftime("%d%H%M"), direction="in", text=text, at=iso(at)))

    def _reply(self, at, **kw):
        return self.st.record_outbound(UID, store.Message(
            id="re-%s" % at.strftime("%d%H%M"), direction="out", text="reply", at=iso(at),
            operator_id="ai:rudi"), **kw)

    def _reach_out(self, at, kind="nudge"):
        """What the runner does: claim against the live record, send, record."""
        ok, why = self.st.claim_outreach(UID, at)
        self.assertTrue(ok, "claim refused at %s: %s" % (at, why))
        return self.st.record_outbound(UID, store.Message(
            id="px-%s" % at.strftime("%d%H%M"), direction="out", text="checking in", at=iso(at),
            operator_id="ai:nudge"), proactive_kind=kind)

    # ------------------------------------------------------------------ the reported burst
    def test_a_nudge_cannot_chase_rudis_own_reply_within_minutes(self):
        """Replay of the incident: reply at 12:54 promising a check-in, nudge due at 12:57."""
        self._in(T("12:50"))
        meta = self._reply(T("12:54"), commitment_at=iso(T("12:57")),
                           commitment_note="the bike ride")
        self.assertEqual(meta.next_proactive_kind, "nudge")
        self.assertEqual(meta.next_proactive_at, iso(T("13:39")),
                         "the promised check-in waits the full 45 minutes after the reply")
        self.assertEqual(store.outreach_block(meta, T("12:57")), "spacing")

    def test_consecutive_reach_outs_are_spaced_45_minutes(self):
        self._in(T("09:00"))
        self._reach_out(T("10:00"))
        meta = self.st.get_meta(UID)
        self.assertEqual(store.outreach_block(meta, T("10:44")), "spacing")
        self.assertEqual(store.outreach_block(meta, T("10:45")), "")
        self.assertEqual(self.st.claim_outreach(UID, T("10:20")), (False, "spacing"))

    # ------------------------------------------------------------------ the cap
    def test_two_unanswered_reach_outs_stop_everything_even_a_promise(self):
        self._in(T("08:00"))
        self._reach_out(T("09:00"))
        self._reach_out(T("10:00"))
        meta = self.st.get_meta(UID)
        self.assertEqual(store.unanswered_outreach(meta), 2)
        self.assertTrue(meta.is_in_window(T("10:30")), "still inside the 24h window")
        meta.commitment_at = iso(T("11:00"))
        self.assertEqual(store.compute_next_proactive(meta, T("10:30")), ("", ""))
        self.assertEqual(self.st.claim_outreach(UID, T("15:00")), (False, "unanswered-cap"))

    def test_the_cap_holds_outside_the_window(self):
        self._in(T("08:00"))
        self._reach_out(T("09:00"))
        self._reach_out(T("10:00"), kind="template")
        later = T("12:00", day=15)
        meta = self.st.get_meta(UID)
        self.assertFalse(meta.is_in_window(later))
        self.assertEqual(store.compute_next_proactive(meta, later), ("", ""),
                         "no template either, however long they stay silent")

    def test_their_reply_resets_both_limits(self):
        self._in(T("08:00"))
        self._reach_out(T("09:00"))
        self._reach_out(T("10:00"))
        meta = self._in(T("10:05"), text="sorry, busy day")
        self.assertEqual(store.unanswered_outreach(meta), 0)
        self.assertEqual(meta.pending_outreach_at, "")
        self.assertEqual(store.outreach_block(meta, T("10:06")), "")
        self.assertTrue(meta.next_proactive_kind, "normal scheduling resumes")

    def test_replies_are_never_counted_but_do_anchor_the_spacing(self):
        self._in(T("08:00"))
        for minute in ("08:01", "08:02", "08:03"):
            meta = self._reply(T(minute))
        self.assertEqual(store.unanswered_outreach(meta), 0)
        self.assertEqual(store.outreach_not_before(meta), T("08:48"))

    # ------------------------------------------------------------------ failure modes
    def test_a_send_that_was_never_recorded_still_counts_and_spaces(self):
        """Claim, send, then the write fails. The runner must not read that as 'nothing sent' and
        fire again on the next 2-minute tick — and two lost sends reach the cap like real ones."""
        self._in(T("08:00"))
        self.assertEqual(self.st.claim_outreach(UID, T("09:00")), (True, ""))    # record lost
        self.assertEqual(self.st.claim_outreach(UID, T("09:02")), (False, "spacing"))
        self.assertEqual(self.st.claim_outreach(UID, T("09:45")), (True, ""))    # record lost
        self.assertEqual(store.unanswered_outreach(self.st.get_meta(UID)), 2)
        self.assertEqual(self.st.claim_outreach(UID, T("11:00")), (False, "unanswered-cap"))

    def test_a_contact_from_before_the_counter_is_counted_from_the_old_markers(self):
        """Live records carry unanswered_outreach=0. The window's nudge marker and the template
        count stand in for it, so a contact already chased twice is not chased a third time."""
        wou = iso(T("12:00", day=14))
        meta = store.ContactMeta(user_id="wa_legacy", phone=PHONE, consent_state="granted",
                                 keep_warm=True, last_inbound_at=iso(T("12:00")),
                                 window_open_until=wou, nudge_sent_for_window=wou,
                                 reengage_count=1)
        self.assertEqual(store.unanswered_outreach(meta), 2)
        self.assertEqual(store.compute_next_proactive(meta, T("13:00")), ("", ""))

    def test_spacing_that_overruns_the_window_becomes_a_template_not_a_late_nudge(self):
        meta = store.ContactMeta(user_id="wa_edge", phone=PHONE, consent_state="granted",
                                 keep_warm=True, timezone="Europe/Brussels",
                                 last_inbound_at=iso(T("12:30", day=12)),
                                 window_open_until=iso(T("12:30")),
                                 last_outbound_at=iso(T("12:00")))
        at, kind = store.compute_next_proactive(meta, T("12:05"))
        self.assertEqual(kind, "template", "a free-form nudge past the window cannot be sent")
        self.assertEqual(at, iso(T("12:45")))


if __name__ == "__main__":
    unittest.main()
