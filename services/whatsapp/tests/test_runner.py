"""
Keep-warm runner (meetrudi-wa-reengage) tests — no network. provider is stubbed to capture
sends; boto3 is the in-memory FakeS3. Synthetic data only.

Run:  python -m unittest discover -s services/whatsapp/tests -v
"""

from __future__ import annotations

import os
import sys
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

_SENT: list = []
provider_stub = types.ModuleType("provider")
provider_stub.send_text = lambda to, body: _SENT.append(("text", to, body))
provider_stub.send_template = lambda to, sid, variables=None: _SENT.append(("template", to, sid))
sys.modules["provider"] = provider_stub

os.environ["DATA_BUCKET"] = "meetrudi-ai-data-test"
os.environ["PSEUDONYMIZE_SALT"] = "test-salt"

import store  # noqa: E402
import reengage  # noqa: E402
import responder  # noqa: E402


def _runner_gen(messages, json_mode=False):
    return {"text": "How's your goal going? 🙂", "model": "fake"}


FUTURE = "2999-01-01T00:00:00+00:00"   # window always open
PAST = "2000-01-01T00:00:00+00:00"     # proactive already due


def _put(uid, **kw):
    base = dict(user_id=uid, phone="+320000000000", consent_state="granted", keep_warm=True,
                locale="en", window_open_until=FUTURE, last_inbound_at=FUTURE, last_message_at=FUTURE,
                next_proactive_at=PAST, next_proactive_kind="nudge")
    base.update(kw)
    reengage.STORE.put_meta(store.ContactMeta(**base))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        _FAKE_S3.__init__()
        _FAKE_S3.put_object(Bucket="meetrudi-ai-data-test", Key="prompts/rudi_guardrails.md", Body=b"GUARD")
        # Bind the responder to our FakeS3 + deterministic LLM for this file's tests.
        responder.s3 = _FAKE_S3
        responder.gateway.generate = _runner_gen
        responder._asset_cache.clear()
        reengage._tpl_cache.clear()
        _SENT.clear()

    def test_due_nudge_is_sent_and_rescheduled(self):
        uid = "wa_due1"
        _put(uid)
        res = reengage.handler({}, None)
        self.assertEqual(res["sent"], 1)
        self.assertEqual(_SENT[0][0], "text")
        self.assertEqual(_SENT[0][1], "+320000000000")
        # nudge marked → won't be re-sent for this window
        self.assertEqual(reengage.STORE.get_meta(uid).nudge_sent_for_window, FUTURE)

    def test_keep_warm_off_is_skipped(self):
        _put("wa_off", keep_warm=False)
        res = reengage.handler({}, None)
        self.assertEqual(res["sent"], 0)
        self.assertEqual(_SENT, [])

    def test_not_due_is_not_sent(self):
        _put("wa_future", next_proactive_at=FUTURE)   # due far in the future
        res = reengage.handler({}, None)
        self.assertEqual(res["sent"], 0)

    def test_template_without_sid_is_skipped(self):
        _put("wa_tmpl", next_proactive_kind="template", window_open_until=PAST)  # window closed
        res = reengage.handler({}, None)
        self.assertEqual(res["sent"], 0)          # no approved SID for the locale in wa_templates.json
        self.assertEqual(_SENT, [])

    def test_template_with_configured_sid_is_sent(self):
        _FAKE_S3.put_object(Bucket="meetrudi-ai-data-test", Key="config/wa_templates.json",
                            Body=b'{"reengage": {"en": ["HXtest123"]}}')
        reengage._tpl_cache.clear()
        _put("wa_tmpl2", next_proactive_kind="template", window_open_until=PAST, locale="en")
        res = reengage.handler({}, None)
        self.assertEqual(res["sent"], 1)
        self.assertEqual(_SENT[0][0], "template")
        self.assertEqual(_SENT[0][2], "HXtest123")   # picked the approved SID for locale=en

    def test_no_consent_skipped(self):
        _put("wa_noconsent", consent_state="unknown")
        res = reengage.handler({}, None)
        self.assertEqual(res["sent"], 0)

    def test_stale_profile_is_reconciled_before_reachout(self):
        uid = "wa_stale"
        _put(uid)   # last_message_at=FUTURE, no profile → stale
        reengage.STORE.append_message(uid, store.Message(id="m1", direction="in",
                                      text="I walked 5k", at="2999-01-01T00:00:00+00:00"))
        reengage.handler({}, None)
        prof = reengage.STORE.get_profile(uid)
        self.assertTrue(prof.get("last_profile_update_at"))   # profile was (re)written
        self.assertTrue(prof.get("most_recent_development"))  # summarized from the newer message

    def test_current_profile_not_reconciled(self):
        uid = "wa_current"
        _put(uid)   # last_message_at=FUTURE (2999-01-01)
        reengage.STORE.write_profile(uid, development="OLD DEV",
                                     now=store.parse_iso("2999-12-31T00:00:00+00:00"))  # newer than messages
        reengage.handler({}, None)
        self.assertEqual(reengage.STORE.get_profile(uid)["most_recent_development"], "OLD DEV")


class CommitmentTests(unittest.TestCase):
    """A check-in Rudi promised out loud must become the next reach-out.

    The failure these guard against is not a crash: Rudi said "I'll check in in 15 minutes",
    the scheduler only knew window mechanics, and the person was met with silence and then a
    template a day later.
    """

    # 14:00 Brussels (CEST, UTC+2) — comfortably inside social hours.
    NOW = "2026-09-13T12:00:00+00:00"
    TOMORROW = "2026-09-14T12:00:00+00:00"

    def setUp(self):
        _FAKE_S3.__init__()
        _FAKE_S3.put_object(Bucket="meetrudi-ai-data-test", Key="prompts/rudi_guardrails.md", Body=b"GUARD")
        responder.s3 = _FAKE_S3
        responder._asset_cache.clear()
        _SENT.clear()

    def _meta(self, **kw):
        base = dict(user_id="wa_c", phone="+320000000000", consent_state="granted",
                    keep_warm=True, locale="en", window_open_until=self.TOMORROW,
                    last_inbound_at=self.NOW, last_message_at=self.NOW)
        base.update(kw)
        return store.ContactMeta(**base)

    # ---------------------------------------------------------------- parsing what Rudi said
    def test_minutes_become_a_time_and_a_note(self):
        when, note = store.commitment_from_signals(
            {"check_in_minutes": 15, "check_in_about": "how the bike ride went"},
            store.parse_iso(self.NOW))
        self.assertEqual(when, "2026-09-13T12:15:00+00:00")
        self.assertEqual(note, "how the bike ride went")

    def test_no_promise_yields_nothing(self):
        for signals in ({}, {"check_in_minutes": None}, {"check_in_minutes": "soon"},
                        {"check_in_minutes": 0}, {"check_in_minutes": -5}, None):
            self.assertEqual(store.commitment_from_signals(signals, store.parse_iso(self.NOW)),
                             ("", ""))

    def test_a_promise_beyond_24h_is_refused_at_capture(self):
        """Past the window Rudi could only send a paid template, which is not a check-in."""
        self.assertEqual(
            store.commitment_from_signals({"check_in_minutes": 24 * 60 + 1},
                                          store.parse_iso(self.NOW)),
            ("", ""))

    # ---------------------------------------------------------------- honouring it
    def test_commitment_becomes_the_next_reachout(self):
        meta = self._meta(commitment_at="2026-09-13T12:15:00+00:00",
                          commitment_note="the bike ride")
        at, kind = store.compute_next_proactive(meta, store.parse_iso(self.NOW))
        self.assertEqual(at, "2026-09-13T12:15:00+00:00")
        self.assertEqual(kind, "nudge")

    def test_commitment_outranks_an_already_spent_nudge(self):
        """The exact shape of the reported bug: the window's nudge was already used, so the
        scheduler fell through to a template a day out and the promise was lost."""
        meta = self._meta(commitment_at="2026-09-13T12:15:00+00:00",
                          nudge_sent_for_window=self.TOMORROW)
        at, kind = store.compute_next_proactive(meta, store.parse_iso(self.NOW))
        self.assertEqual(kind, "nudge")
        self.assertEqual(at, "2026-09-13T12:15:00+00:00")

    def test_a_promise_landing_in_quiet_hours_waits_for_morning(self):
        # 22:00 Brussels is inside the 21:30-06:30 quiet window → pushed to 06:30 local (04:30Z).
        meta = self._meta(commitment_at="2026-09-13T20:00:00+00:00",
                          window_open_until="2026-09-14T12:00:00+00:00")
        at, kind = store.compute_next_proactive(meta, store.parse_iso("2026-09-13T19:00:00+00:00"))
        self.assertEqual(at, "2026-09-14T04:30:00+00:00")
        self.assertEqual(kind, "nudge")

    def test_a_promise_past_the_window_does_not_schedule_a_check_in(self):
        meta = self._meta(commitment_at="2026-09-13T12:15:00+00:00",
                          window_open_until="2026-09-13T12:10:00+00:00")
        at, _ = store.compute_next_proactive(meta, store.parse_iso(self.NOW))
        self.assertNotEqual(at, "2026-09-13T12:15:00+00:00")

    # ---------------------------------------------------------------- lifecycle
    def test_keeping_the_promise_clears_it(self):
        st = store.ConversationStore(_FAKE_S3, "meetrudi-ai-data-test")
        st.put_meta(self._meta(commitment_at="2026-09-13T12:15:00+00:00",
                               commitment_note="the bike ride"))
        st.record_outbound("wa_c", store.Message(id="m1", direction="out", text="How did it go?",
                                                 at="2026-09-13T12:15:00+00:00"),
                           proactive_kind="nudge")
        meta = st.get_meta("wa_c")
        self.assertEqual(meta.commitment_at, "", "a kept promise must not fire again")
        self.assertEqual(meta.commitment_note, "")

    def test_a_new_user_turn_supersedes_the_old_promise(self):
        st = store.ConversationStore(_FAKE_S3, "meetrudi-ai-data-test")
        st.put_meta(self._meta(commitment_at="2026-09-13T12:15:00+00:00",
                               commitment_note="the bike ride"))
        st.record_inbound("wa_c", "+320000000000",
                          store.Message(id="m2", direction="in", text="already done!",
                                        at="2026-09-13T12:05:00+00:00"))
        self.assertEqual(st.get_meta("wa_c").commitment_at, "")

    def test_outbound_records_a_new_promise(self):
        st = store.ConversationStore(_FAKE_S3, "meetrudi-ai-data-test")
        st.put_meta(self._meta())
        st.record_outbound("wa_c", store.Message(id="m3", direction="out", text="I'll check back",
                                                 at=self.NOW),
                           commitment_at="2026-09-13T12:15:00+00:00",
                           commitment_note="the bike ride")
        meta = st.get_meta("wa_c")
        self.assertEqual(meta.commitment_at, "2026-09-13T12:15:00+00:00")
        self.assertEqual(meta.next_proactive_at, "2026-09-13T12:15:00+00:00")
        self.assertEqual(meta.next_proactive_kind, "nudge")

    # ---------------------------------------------------------------- what Rudi is told
    def test_the_reachout_is_told_what_was_promised(self):
        """A generic 'how's it going' after promising to ask about the bike ride reads as
        having forgotten — so the promise has to reach the prompt."""
        seen = {}

        def capture(messages, json_mode=False):
            seen["system"] = messages[0]["content"]
            return {"text": "How did the bike ride go?", "model": "fake"}

        responder.gateway.generate = capture
        reengage.STORE = store.ConversationStore(_FAKE_S3, "meetrudi-ai-data-test")
        reengage.STORE.put_meta(self._meta(
            user_id="wa_promise", next_proactive_at=PAST, next_proactive_kind="nudge",
            window_open_until=FUTURE, last_inbound_at=FUTURE, last_message_at=FUTURE,
            commitment_at=PAST, commitment_note="how the 10-minute bike ride went"))
        reengage.handler({}, None)
        self.assertIn("how the 10-minute bike ride went", seen.get("system", ""))
        self.assertTrue(_SENT, "the promised check-in must actually be sent")


if __name__ == "__main__":
    unittest.main()
