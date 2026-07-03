"""
Processor tests (Block 4) — inbound is persisted to the store; operator mode sends nothing.

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
os.environ["AI_RESPONDER"] = "false"   # this suite covers the persist-only (operator) path

import store  # noqa: E402
import processor  # noqa: E402


def _sqs_event(**msg):
    return {"Records": [{"body": json.dumps(msg)}]}


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        _FAKE_S3.__init__()

    def test_inbound_persisted_and_window_opened(self):
        processor.handler(_sqs_event(
            user_phone="+32470000000", type="text", text="hello", provider_msg_id="SM1"), None)
        uid = store.user_id("+32470000000", "test-salt")
        thread = processor.STORE.list_messages(uid)
        self.assertEqual([m.text for m in thread], ["hello"])
        meta = processor.STORE.get_meta(uid)
        self.assertEqual(meta.unread_count, 1)
        self.assertTrue(meta.is_in_window())
        self.assertEqual(meta.phone, "+32470000000")   # PII kept server-side in meta

    def test_media_inbound_recorded(self):
        processor.handler(_sqs_event(
            user_phone="+32470000001", type="image", text="",
            media=[{"url": "https://api.twilio.com/x", "content_type": "image/jpeg"}],
            provider_msg_id="SM2"), None)
        uid = store.user_id("+32470000001", "test-salt")
        thread = processor.STORE.list_messages(uid)
        self.assertEqual(thread[0].type, "image")
        self.assertEqual(len(thread[0].media), 1)

    def test_missing_phone_skipped(self):
        processor.handler(_sqs_event(type="text", text="x", provider_msg_id="SM3"), None)
        # nothing created
        self.assertEqual(processor.STORE.list_conversation_ids(), [])

    def test_bad_record_does_not_raise(self):
        processor.handler({"Records": [{"body": "{not json"}]}, None)  # should swallow + continue


if __name__ == "__main__":
    unittest.main()
