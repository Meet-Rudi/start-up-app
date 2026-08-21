"""
De-identification on the WhatsApp path — the wiring, not the library.

test_deid.py already covers deid.py itself. What was missing is that NOTHING imported it: the
protection was written, tested and switched off. These tests assert the live path, i.e. that a
raw identifier cannot reach S3, the operator console, or a model, and that the alias map does
not outlive the 24h window that produced it.

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
os.environ.setdefault("DATA_BUCKET", BUCKET)
os.environ.setdefault("CONTACT_SALT", "test-salt")

_FAKE_S3 = FakeS3()
boto3_stub = types.ModuleType("boto3")
boto3_stub.client = lambda name, *a, **k: _FAKE_S3
sys.modules["boto3"] = boto3_stub

_SENT: list = []
provider_stub = types.ModuleType("provider")
provider_stub.send_text = lambda to, body: _SENT.append(body)
provider_stub.send_template = lambda to, sid, variables=None: _SENT.append(("template", sid))
provider_stub.fetch_media = lambda url: b""
sys.modules["provider"] = provider_stub

import deid       # noqa: E402
import store      # noqa: E402
import responder  # noqa: E402

# processor is imported LAZILY, inside setUp, and that is deliberate.
#
# `unittest discover` loads every module in this directory into one process before running
# anything, and processor.py does two things at import: it reads AI_RESPONDER, and it builds
# STORE bound to whichever FakeS3 the boto3 stub returns at that instant. Whoever imports it
# first therefore decides both, for every module in the run.
#
# Importing it here at module scope made this file the first importer — it sorts before
# test_processor.py — which turned that module's own setUp into a no-op against a dead client
# and broke thirteen of its tests, while every module still passed in isolation. Not racing to
# be the importer is the only fix that does not depend on filenames.
processor = None

_REPLY = {"text": "Thanks, noted."}


def _fake_respond(state, text, locale="en", personality_block=""):
    return (_REPLY["text"], dict(state or {}, phase="goal", history=[]),
            {"phase": "goal", "lang": locale})


class WiringCase(unittest.TestCase):
    """Patches responder.respond per-test rather than at import.

    `unittest discover` loads every module into ONE process before running anything, so a
    module-level monkey-patch leaks into every other file. Doing it at module scope here broke
    13 unrelated tests in test_responder and test_processor while each still passed in
    isolation — which is the worst shape a test failure can take.
    """

    def setUp(self):
        global processor
        if processor is None:
            import processor as _p          # already loaded by now; this just binds the name
            processor = _p

        _SENT.clear()
        _REPLY["text"] = "Thanks, noted."
        self._real_respond = responder.respond
        self._real_ai_flag = processor.AI_RESPONDER
        responder.respond = _fake_respond
        processor.AI_RESPONDER = True          # these tests exercise the AI reply path

        # Every module here builds its own FakeS3 and stubs sys.modules["boto3"], so the LIVE
        # client is whichever existed when processor was first imported — i.e. it depends on
        # alphabetical file order. Rather than depend on that, borrow the store for the length
        # of a test and hand it straight back, so neither module can disturb the other.
        self._real_s3 = processor.STORE._s3
        self._real_store_s3 = getattr(store, "s3", None)
        processor.STORE._s3 = _FAKE_S3
        store.s3 = _FAKE_S3
        _FAKE_S3.__init__()

    def tearDown(self):
        responder.respond = self._real_respond
        processor.AI_RESPONDER = self._real_ai_flag
        processor.STORE._s3 = self._real_s3
        if self._real_store_s3 is not None:
            store.s3 = self._real_store_s3


def _inbound(phone, text):
    processor.handler({"Records": [{"body": json.dumps(
        {"user_phone": phone, "text": text, "type": "text",
         "provider_msg_id": "SM%d" % len(_SENT)})}]}, None)
    return store.user_id(phone, processor.SALT)


def _stored(uid):
    """Everything persisted for this contact, as one blob."""
    return json.dumps({k: v.decode("utf-8", "replace")
                       for k, v in _FAKE_S3._store[BUCKET].items() if uid in k})


class Ingest(WiringCase):

    def test_national_number_never_written(self):
        uid = _inbound("+32470000001", "my rijksregisternummer is 85.07.30-033.28")
        blob = _stored(uid)
        self.assertNotIn("85.07.30-033.28", blob)
        self.assertNotIn("8507300332", blob)
        self.assertIn(deid.LBL_NATIONAL_ID, blob)

    def test_email_and_card_never_written(self):
        uid = _inbound("+32470000002", "beata@example.com or my card 4111 1111 1111 1111")
        blob = _stored(uid)
        self.assertNotIn("beata@example.com", blob)
        self.assertNotIn("4111", blob)

    def test_scrubbed_before_persistence_not_after(self):
        """record_inbound must receive clean text — there is no window where the raw message
        sits in S3 waiting to be cleaned."""
        seen = {}
        original = store.ConversationStore.record_inbound
        def spy(self, uid, phone, msg):
            seen["text"] = msg.text
            return original(self, uid, phone, msg)
        store.ConversationStore.record_inbound = spy
        try:
            _inbound("+32470000003", "my number is 85.07.30-033.28")
        finally:
            store.ConversationStore.record_inbound = original
        self.assertNotIn("85.07.30-033.28", seen["text"])

    def test_model_never_receives_the_identifier(self):
        seen = {}
        original = responder.respond
        def spy(state, text, locale="en", personality_block=""):
            seen["text"] = text
            return original(state, text, locale, personality_block)
        responder.respond = spy
        try:
            _inbound("+32470000004", "my number is 85.07.30-033.28")
        finally:
            responder.respond = original
        self.assertNotIn("85.07.30-033.28", seen["text"])


class ThirdPartyNames(WiringCase):

    def test_name_is_masked_in_the_message_body(self):
        """The conversation itself must be non-identifying. The vault holds the mapping for the
        life of the session — that is what expiry is for, tested below."""
        uid = _inbound("+32470000010", "I walk with my daughter Anneke on Tuesdays")
        msgs = [v.decode("utf-8") for k, v in _FAKE_S3._store[BUCKET].items()
                if uid in k and "/messages/" in k]
        self.assertTrue(msgs)
        self.assertNotIn("Anneke", " ".join(msgs))
        self.assertIn("Person_A", " ".join(msgs))

    def test_name_is_restored_before_sending(self):
        """The patient must read a natural message, not a placeholder."""
        _inbound("+32470000011", "I walk with my daughter Anneke")
        _REPLY["text"] = "Lovely — say hi to <| Person_A |> for me."
        _inbound("+32470000011", "will do")
        self.assertNotIn("Person_A", _SENT[-1])
        self.assertFalse(deid.has_placeholder(_SENT[-1]))

    def test_unresolved_placeholder_never_reaches_the_phone(self):
        """If the vault cannot resolve it, send a neutral word rather than raw markup."""
        _REPLY["text"] = "Say hi to <| Person_Z |> for me."
        _inbound("+32470000012", "hello")
        self.assertNotIn("Person_Z", _SENT[-1])
        self.assertNotIn("<|", _SENT[-1])


class SessionBoundary(WiringCase):
    """Session = the 24h WhatsApp window. The alias map must not outlive it, or a stored
    message stays re-identifiable long after the conversation that produced it."""

    def test_vault_persists_within_the_window(self):
        phone = "+32470000020"
        uid = _inbound(phone, "my daughter Anneke walks with me")
        first = processor.STORE.get_meta(uid).alias_vault
        self.assertTrue(first, "vault should be kept while the window is open")
        _inbound(phone, "she came again today")
        self.assertEqual(processor.STORE.get_meta(uid).alias_vault.get("mapping"),
                         first.get("mapping"), "same session must keep the same aliases")

    def test_new_window_resets_the_vault(self):
        phone = "+32470000021"
        uid = _inbound(phone, "my daughter Anneke walks with me")
        meta = processor.STORE.get_meta(uid)
        self.assertTrue(meta.alias_vault)

        # Push the window closed: last inbound 48h ago.
        past = store.to_iso(store.now_dt() - datetime.timedelta(hours=48))
        meta.last_inbound_at = past
        meta.window_open_until = store.window_open_until(past)
        processor.STORE.put_meta(meta)
        self.assertFalse(meta.is_in_window())

        _inbound(phone, "hello again")
        after = processor.STORE.get_meta(uid).alias_vault
        self.assertNotIn("Anneke", json.dumps(after),
                         "a new window must start from a fresh vault")

    def test_dormant_contact_has_the_vault_swept(self):
        """The processor only resets on the NEXT message. Someone who never writes again would
        keep the mapping indefinitely, so the scheduled runner expires it instead."""
        import reengage
        phone = "+32470000023"
        uid = _inbound(phone, "my daughter Anneke walks with me")
        meta = processor.STORE.get_meta(uid)
        self.assertIn("Anneke", json.dumps(meta.alias_vault))

        past = store.to_iso(store.now_dt() - datetime.timedelta(hours=48))
        meta.last_inbound_at = past
        meta.window_open_until = store.window_open_until(past)
        processor.STORE.put_meta(meta)

        reengage.STORE = processor.STORE
        reengage._expire_alias_vault(processor.STORE.get_meta(uid))
        self.assertEqual(processor.STORE.get_meta(uid).alias_vault, {})
        self.assertNotIn("Anneke", _stored(uid))

    def test_no_direct_identifier_ever_lands_in_the_vault(self):
        """The vault holds names only. Direct identifiers are redacted irreversibly upstream and
        must never become a recoverable alias."""
        phone = "+32470000022"
        uid = _inbound(phone, "I am 85.07.30-033.28 and my sister is Anneke")
        vault = json.dumps(processor.STORE.get_meta(uid).alias_vault)
        self.assertNotIn("85.07.30-033.28", vault)
        self.assertNotIn("8507300332", vault)


if __name__ == "__main__":
    unittest.main()
