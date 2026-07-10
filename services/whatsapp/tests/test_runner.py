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


if __name__ == "__main__":
    unittest.main()
