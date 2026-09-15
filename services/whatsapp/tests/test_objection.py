"""
Inbound objection gate — the wrong-number safety net.

The scenario these exist for: a digit is mistyped at registration, Rudi starts messaging a
stranger about their health twice a week, and the stranger has no account and no way to stop it.
The only signal available is what they write back.

Two failure modes are tested with equal weight, because they are both expensive:

  MISSING an objection  — we keep messaging somebody who told us to stop. This is the one that
                          becomes a complaint, a block, and a damaged number rating (§3).
  INVENTING an objection — we freeze a real patient mid-coaching. A health coach hears "ik wil
                          stoppen met roken" constantly, and a naive substring match on "stop"
                          would silence exactly the conversations that are going well.

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

BUCKET = "meetrudi-ai-data-test"
os.environ.setdefault("DATA_BUCKET", BUCKET)
os.environ["PSEUDONYMIZE_SALT"] = "test-salt"
os.environ["AI_RESPONDER"] = "false"           # the gate must fire in operator mode too
os.environ["OBJECTION_CLASSIFIER"] = "false"   # model layer is injected per-test, never called

_FAKE_S3 = FakeS3()
if "boto3" not in sys.modules:
    _boto3 = types.ModuleType("boto3")
    _boto3.client = lambda *a, **k: _FAKE_S3
    sys.modules["boto3"] = _boto3

import i18n         # noqa: E402
import store        # noqa: E402
import objection    # noqa: E402
import processor    # noqa: E402


class Lexicon(unittest.TestCase):
    """Deterministic layer. No model, no network, no excuses."""

    def _fires(self, text, kind=None):
        d = objection.check_lexicon(text)
        self.assertTrue(d.fired, "should have fired: %r" % text)
        if kind:
            self.assertEqual(d.kind, kind, "%r -> %s" % (text, d.kind))
        return d

    def _quiet(self, text):
        d = objection.check_lexicon(text)
        self.assertFalse(d.fired, "should NOT have fired: %r (matched %r)" % (text, d.evidence))

    # ---- the wrong-number case, which is what this whole gate is for -------------------
    def test_flemish_wrong_number(self):
        for text in ("Verkeerd nummer!", "Sorry, wie is dit?", "Ik ken u niet",
                     "ik heb me niet ingeschreven voor iets",
                     "je hebt de verkeerde persoon denk ik"):
            self._fires(text, objection.WRONG_NUMBER)

    def test_wrong_number_in_every_locale_we_serve(self):
        for text in ("You have the wrong number", "Wer sind Sie?",
                     "Je ne vous connais pas", "mauvais numero"):
            self._fires(text, objection.WRONG_NUMBER)

    def test_locale_on_file_is_not_trusted(self):
        """A wrong number's stored locale came from the wrong registration, so the lexicon is
        scanned in full regardless. A contact marked nl objecting in English must still fire."""
        self._fires("who is this? I never signed up", objection.WRONG_NUMBER)

    # ---- opt-out --------------------------------------------------------------------
    def test_bare_stop_is_an_opt_out(self):
        for text in ("STOP", "stop", " Stop! ", "afmelden"):
            self._fires(text, objection.UNSUBSCRIBE)

    def test_phrased_opt_out(self):
        for text in ("stuur me geen berichten meer aub", "please stop contacting me",
                     "keine Nachrichten mehr bitte"):
            self._fires(text, objection.UNSUBSCRIBE)

    def test_hostile_outranks_plain_opt_out(self):
        d = self._fires("laat me met rust, dit is spam")
        self.assertEqual(d.kind, objection.HOSTILE)

    # ---- the false positives that would break real coaching -------------------------
    def test_a_health_goal_about_stopping_is_not_an_objection(self):
        """The trap. Half of health coaching is about stopping something."""
        for text in ("ik wil stoppen met roken",
                     "Ik moet stoppen met snoepen 's avonds",
                     "I want to stop eating late at night",
                     "ich möchte mit dem Rauchen aufhören",
                     "mijn doel is stoppen met alcohol"):
            self._quiet(text)

    def test_ordinary_replies_are_not_objections(self):
        for text in ("nee", "no", "vandaag even niet", "ik ben niet gemotiveerd",
                     "Wie kan mij daarbij helpen?", "Quite good. How was yours?",
                     "Ging goed", "ik ben er klaar voor"):
            self._quiet(text)

    def test_empty_and_junk_are_quiet(self):
        for text in ("", None, "   ", "👍"):
            self._quiet(text)

    # ---- normalization --------------------------------------------------------------
    def test_accents_caps_and_punctuation_do_not_hide_an_objection(self):
        self._fires("ARRÊTEZ DE ME CONTACTER!!!", objection.UNSUBSCRIBE)
        self._fires("Wie... is DIT???", objection.WRONG_NUMBER)


class Classifier(unittest.TestCase):
    """Advisory layer. It may ADD a detection; it may never remove one, and it may never break
    the gate by being unavailable."""

    def _gen(self, payload):
        def generate(messages, json_mode=False):
            return {"text": json.dumps(payload), "model": "stub"}
        return generate

    def test_catches_what_the_lexicon_cannot(self):
        text = "I think you may have mixed me up with somebody else"
        self.assertFalse(objection.check_lexicon(text).fired)   # genuinely not in the list
        d = objection.detect(text, generate=self._gen(
            {"objection": True, "kind": "wrong_number", "reason": "mixed me up"}))
        self.assertTrue(d.fired)
        self.assertEqual(d.kind, objection.WRONG_NUMBER)
        self.assertEqual(d.source, "classifier")

    def test_a_dead_model_cannot_open_the_gate(self):
        def boom(messages, json_mode=False):
            raise RuntimeError("all endpoints rate-limited")
        self.assertFalse(objection.detect("some ambiguous text", generate=boom).fired)
        # ...but the lexicon floor still holds while the model is down.
        self.assertTrue(objection.detect("verkeerd nummer", generate=boom).fired)

    def test_malformed_model_output_is_not_an_objection(self):
        def junk(messages, json_mode=False):
            return {"text": "I'm afraid I can't do that"}
        self.assertFalse(objection.detect("hello there", generate=junk).fired)

    def test_lexicon_hit_short_circuits_the_model(self):
        calls = []

        def counting(messages, json_mode=False):
            calls.append(1)
            return {"text": "{}"}
        objection.detect("STOP", generate=counting)
        self.assertEqual(calls, [], "a lexicon hit must not cost a model call")

    def test_negative_verdict_is_respected(self):
        d = objection.detect("ik wil stoppen met roken", generate=self._gen({"objection": False}))
        self.assertFalse(d.fired)


class Freezing(unittest.TestCase):
    def setUp(self):
        _FAKE_S3.__init__()
        # See the note in test_processor.setUp: `processor` is shared across the discover run and
        # binds its S3 client at import time, so each suite points it at its own fake.
        processor._s3 = _FAKE_S3
        processor.STORE = store.ConversationStore(_FAKE_S3, BUCKET)
        self.sent = []
        self._real_send = processor.provider.send_text
        processor.provider.send_text = lambda to, body: self.sent.append(body)

    def tearDown(self):
        processor.provider.send_text = self._real_send
        _FAKE_S3.__init__()

    def _inbound(self, phone, text, sid):
        processor.handler({"Records": [{"body": json.dumps(
            {"user_phone": phone, "type": "text", "text": text,
             "provider_msg_id": sid})}]}, None)
        return store.user_id(phone, "test-salt")

    def test_objection_freezes_acknowledges_once_and_alerts(self):
        uid = self._inbound("+32470000010", "Verkeerd nummer, ik ken u niet", "SM10")
        meta = processor.STORE.get_meta(uid)
        self.assertEqual(meta.status, "frozen")
        self.assertEqual(meta.frozen_kind, objection.WRONG_NUMBER)
        self.assertFalse(meta.keep_warm)
        # Nothing further may be scheduled, ever, without an operator.
        self.assertEqual(meta.next_proactive_at, "")
        self.assertEqual(meta.next_proactive_kind, "")
        # Exactly one canned line went out — and it is the canned one, not a generated reply.
        # A never-seen contact has no evidence of language yet, so this is the "en" default.
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0], i18n.t("frozen_ack", "en"))
        # A human has something to work from.
        alerts_written = [k for k in _FAKE_S3._store.get(BUCKET, {})
                          if k.startswith("operator-alerts/")]
        self.assertEqual(len(alerts_written), 1)
        record = json.loads(_FAKE_S3._store[BUCKET][alerts_written[0]])
        self.assertEqual(record["kind"], "conversation_frozen")
        self.assertEqual(record["detection"]["kind"], objection.WRONG_NUMBER)
        self.assertEqual(record["status"], "open")

    def test_ack_uses_the_language_on_file(self):
        """The realistic wrong-number case: a nl-BE registration with a mistyped digit, so the
        contact carries a Dutch locale before the stranger ever writes back."""
        uid = self._inbound("+32470000020", "hallo", "SM20")
        meta = processor.STORE.get_meta(uid)
        meta.locale = "nl"
        processor.STORE.put_meta(meta)
        self.sent.clear()
        self._inbound("+32470000020", "sorry, verkeerd nummer", "SM21")
        self.assertEqual(self.sent, [i18n.t("frozen_ack", "nl")])
        self.assertIn("geen berichten meer", self.sent[0])

    def test_a_frozen_contact_is_never_answered_again(self):
        uid = self._inbound("+32470000011", "wie is dit", "SM11")
        self.sent.clear()
        self._inbound("+32470000011", "hallo? iemand daar?", "SM12")
        self.assertEqual(self.sent, [], "a frozen thread must stay silent")
        # ...but the message is still stored, because that is what gets investigated.
        texts = [m.text for m in processor.STORE.list_messages(uid)]
        self.assertIn("hallo? iemand daar?", texts)
        self.assertEqual(processor.STORE.get_meta(uid).status, "frozen")

    def test_media_from_a_frozen_contact_gets_no_acknowledgement(self):
        self._inbound("+32470000012", "verkeerd nummer", "SM13")
        self.sent.clear()
        processor.handler({"Records": [{"body": json.dumps(
            {"user_phone": "+32470000012", "type": "image", "text": "",
             "media": [{"url": "u", "content_type": "image/jpeg"}],
             "provider_msg_id": "SM14"})}]}, None)
        self.assertEqual(self.sent, [])

    def test_an_ordinary_message_is_untouched(self):
        uid = self._inbound("+32470000013", "ik wil stoppen met roken", "SM15")
        self.assertEqual(processor.STORE.get_meta(uid).status, "active")
        self.assertEqual([k for k in _FAKE_S3._store.get(BUCKET, {})
                          if k.startswith("operator-alerts/")], [])

    def test_operator_can_unfreeze(self):
        uid = self._inbound("+32470000014", "stop", "SM16")
        self.assertEqual(processor.STORE.get_meta(uid).status, "frozen")
        meta = processor.STORE.unfreeze(uid, operator_id="filip")
        self.assertEqual(meta.status, "active")
        self.assertTrue(meta.keep_warm)
        self.assertEqual(meta.frozen_kind, "")

    def test_freezing_is_not_the_same_as_revoking_consent(self):
        """Consent is a decision a known user made. A freeze means we do not yet know who this
        is — collapsing the two would close a wrong-number incident as a routine opt-out."""
        uid = self._inbound("+32470000015", "verkeerde persoon", "SM17")
        self.assertEqual(processor.STORE.get_meta(uid).consent_state, "granted")
        self.assertEqual(processor.STORE.get_meta(uid).status, "frozen")


if __name__ == "__main__":
    unittest.main()
