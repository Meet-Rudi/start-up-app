"""
Contract + unit tests for ConversationStore (Block 0/1) — zero external deps.

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
from store import ConversationStore, ContactMeta, Message, user_id, window_open_until  # noqa: E402
from fake_s3 import FakeS3  # noqa: E402

BUCKET = "meetrudi-ai-data-test"
SALT = "test-salt"
PHONE = "+32470000000"


def _iso(y, mo, d, h=12, mi=0):
    return store.to_iso(datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.timezone.utc))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.store = ConversationStore(self.s3, BUCKET)
        self.uid = user_id(PHONE, SALT)

    # -- pseudonymization ------------------------------------------------
    def test_user_id_is_stable_and_pii_free(self):
        self.assertEqual(user_id(PHONE, SALT), user_id(PHONE, SALT))
        self.assertNotEqual(user_id(PHONE, SALT), user_id("+32470000001", SALT))
        self.assertTrue(self.uid.startswith("wa_"))
        self.assertNotIn("470000000", self.uid)  # raw phone never appears in the id

    # -- inbound persistence + window ------------------------------------
    def test_record_inbound_creates_contact_and_opens_window(self):
        at = _iso(2026, 7, 1, 10, 0)
        msg = Message(id="SM1", direction="in", text="hi rudi", at=at)
        meta = self.store.record_inbound(self.uid, PHONE, msg)

        self.assertEqual(meta.phone, PHONE)
        self.assertEqual(meta.unread_count, 1)
        self.assertEqual(meta.last_direction, "in")
        self.assertEqual(meta.last_message_preview, "hi rudi")
        self.assertEqual(meta.window_open_until, window_open_until(at))

        # persisted and reloadable
        reloaded = self.store.get_meta(self.uid)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.phone, PHONE)

    def test_window_open_and_expired(self):
        at = _iso(2026, 7, 1, 10, 0)
        self.store.record_inbound(self.uid, PHONE, Message(id="SM1", direction="in", text="hi", at=at))
        meta = self.store.get_meta(self.uid)

        just_after = datetime.datetime(2026, 7, 1, 20, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(meta.is_in_window(just_after))          # < 24h
        long_after = datetime.datetime(2026, 7, 3, 10, 0, tzinfo=datetime.timezone.utc)
        self.assertFalse(meta.is_in_window(long_after))         # > 24h

    def test_no_window_means_closed(self):
        self.assertFalse(ContactMeta(user_id="wa_x").is_in_window())

    # -- outbound clears unread ------------------------------------------
    def test_record_outbound_clears_unread(self):
        self.store.record_inbound(self.uid, PHONE, Message(id="SM1", direction="in", text="hi", at=_iso(2026, 7, 1, 10, 0)))
        self.store.record_inbound(self.uid, PHONE, Message(id="SM2", direction="in", text="you there?", at=_iso(2026, 7, 1, 10, 5)))
        self.assertEqual(self.store.get_meta(self.uid).unread_count, 2)

        meta = self.store.record_outbound(self.uid, Message(id="OUT1", direction="out", text="hey!", at=_iso(2026, 7, 1, 10, 6), operator_id="op-anna"))
        self.assertEqual(meta.unread_count, 0)
        self.assertEqual(meta.last_direction, "out")

    def test_mark_read(self):
        self.store.record_inbound(self.uid, PHONE, Message(id="SM1", direction="in", text="hi", at=_iso(2026, 7, 1, 10, 0)))
        self.store.mark_read(self.uid)
        self.assertEqual(self.store.get_meta(self.uid).unread_count, 0)

    # -- thread ordering + isolation -------------------------------------
    def test_thread_is_chronological(self):
        self.store.record_inbound(self.uid, PHONE, Message(id="A", direction="in", text="first", at=_iso(2026, 7, 1, 10, 0)))
        self.store.record_outbound(self.uid, Message(id="B", direction="out", text="second", at=_iso(2026, 7, 1, 10, 1)))
        self.store.record_inbound(self.uid, PHONE, Message(id="C", direction="in", text="third", at=_iso(2026, 7, 1, 10, 2)))
        thread = self.store.list_messages(self.uid)
        self.assertEqual([m.text for m in thread], ["first", "second", "third"])
        self.assertEqual([m.direction for m in thread], ["in", "out", "in"])

    def test_conversations_are_isolated(self):
        uid2 = user_id("+32470000009", SALT)
        self.store.record_inbound(self.uid, PHONE, Message(id="A", direction="in", text="mine", at=_iso(2026, 7, 1, 10, 0)))
        self.store.record_inbound(uid2, "+32470000009", Message(id="B", direction="in", text="theirs", at=_iso(2026, 7, 1, 10, 0)))
        self.assertEqual([m.text for m in self.store.list_messages(self.uid)], ["mine"])
        self.assertEqual([m.text for m in self.store.list_messages(uid2)], ["theirs"])

    # -- polling cursor --------------------------------------------------
    def test_since_cursor_returns_only_new(self):
        self.store.record_inbound(self.uid, PHONE, Message(id="A", direction="in", text="one", at=_iso(2026, 7, 1, 10, 0)))
        cursor = self.store.latest_cursor(self.uid)
        self.store.record_inbound(self.uid, PHONE, Message(id="B", direction="in", text="two", at=_iso(2026, 7, 1, 10, 1)))
        new = self.store.list_messages(self.uid, since=cursor)
        self.assertEqual([m.text for m in new], ["two"])

    # -- roster ----------------------------------------------------------
    def test_roster_lists_all_sorted_recent_first(self):
        uid2 = user_id("+32470000009", SALT)
        self.store.record_inbound(self.uid, PHONE, Message(id="A", direction="in", text="older", at=_iso(2026, 7, 1, 9, 0)))
        self.store.record_inbound(uid2, "+32470000009", Message(id="B", direction="in", text="newer", at=_iso(2026, 7, 1, 11, 0)))
        roster = self.store.list_roster()
        self.assertEqual(len(roster), 2)
        self.assertEqual(roster[0].user_id, uid2)   # most recent first
        self.assertEqual(roster[1].user_id, self.uid)

    def test_media_preview(self):
        self.store.record_inbound(self.uid, PHONE, Message(id="A", direction="in", type="image", text="", at=_iso(2026, 7, 1, 10, 0), media=[{"url": "x"}]))
        self.assertIn("Photo", self.store.get_meta(self.uid).last_message_preview)

    # -- round-trip serialization ----------------------------------------
    def test_message_roundtrip(self):
        m = Message(id="A", direction="out", text="hi", at=_iso(2026, 7, 1, 10, 0), operator_id="op1")
        self.assertEqual(Message.from_dict(m.to_dict()), m)


if __name__ == "__main__":
    unittest.main()
