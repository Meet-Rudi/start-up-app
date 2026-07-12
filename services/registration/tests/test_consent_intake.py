"""
Tests for meetrudi-consent-intake — validation, phone normalization, key naming, consent gate,
honeypot, echo challenge. No boto3, no network. Synthetic data only (§8).

Run:  python -m unittest discover -s services/registration/tests -v
"""

from __future__ import annotations

import os
import sys
import json
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket, Key, Body, **_):
        self.puts.append({"Bucket": Bucket, "Key": Key,
                          "Body": Body.decode("utf-8") if isinstance(Body, bytes) else Body})
        return {}


_FAKE_S3 = FakeS3()
boto3_stub = types.ModuleType("boto3")
boto3_stub.client = lambda name, *a, **k: _FAKE_S3
sys.modules["boto3"] = boto3_stub
os.environ["DATA_BUCKET"] = "meetrudi-ai-data-test"
os.environ["PSEUDONYMIZE_SALT"] = "test-salt"

import consent_intake  # noqa: E402


def _answers(**over):
    a = {"first_name": "Jan", "mobile_number": "0470 12 34 56", "whatsapp_number_confirmed": True,
         "email": "jan@example.be",
         "year_of_birth": 1975, "gender": "man", "municipality": "Leuven",
         "cm_member_t2d": True, "help_areas": ["Gezonder eten", "Beter slapen"],
         "confidence_self_improve": 6, "energy_past_week": 4, "healthy_living_past_week": 5,
         "support_felt": 7, "active_days_past_week": 3,
         "prior_digital_tools": "Ja, maar ik haakte af", "notes_for_rudi": ""}
    a.update(over)
    return a


def _payload(**over):
    p = {"form_language": "nl", "answers": _answers(),
         "consents": {"consent_health_data_processing": True, "consent_followup_contact": False}}
    p.update(over)
    return p


def _event(payload, method="POST", ip="1.2.3.4"):
    return {"requestContext": {"http": {"method": method, "sourceIp": ip}},
            "headers": {"user-agent": "pytest"},
            "body": json.dumps(payload) if payload is not None else None}


class ConsentIntakeTests(unittest.TestCase):
    def setUp(self):
        _FAKE_S3.puts.clear()

    # -- happy path ------------------------------------------------------
    def test_valid_submission_stored(self):
        r = consent_intake.handler(_event(_payload()), None)
        self.assertEqual(r["statusCode"], 201)
        self.assertEqual(len(_FAKE_S3.puts), 1)
        put = _FAKE_S3.puts[0]
        self.assertTrue(put["Key"].startswith("registrations/consent_documents/"))
        self.assertTrue(put["Key"].endswith(".json"))
        rec = json.loads(put["Body"])
        # phone normalized, EN keys, NL values preserved, pseudonym present
        self.assertEqual(rec["identity"]["phone_e164"], "+32470123456")
        self.assertEqual(rec["answers"]["gender"], "man")
        self.assertTrue(rec["identity"]["pseudonym"].startswith("wa_"))
        self.assertTrue(rec["consents"]["health_data_processing"]["granted"])

    def test_key_has_last4_and_timestamp(self):
        consent_intake.handler(_event(_payload()), None)
        name = _FAKE_S3.puts[0]["Key"].rsplit("/", 1)[1]
        parts = name[:-5].split("_")   # <hmac>_<last4>_<yyyymmdd>_<hhmmss>
        self.assertEqual(parts[1], "3456")               # last 4 of +32470123456
        self.assertEqual(len(parts[0]), 64)              # full sha256 hex
        self.assertRegex(parts[2] + "_" + parts[3], r"^\d{8}_\d{6}$")

    def test_pseudonym_matches_whatsapp_scheme(self):
        r = consent_intake.handler(_event(_payload()), None)
        rec = json.loads(_FAKE_S3.puts[0]["Body"])
        import hmac as _h, hashlib
        expect = "wa_" + _h.new(b"test-salt", b"+32470123456", hashlib.sha256).hexdigest()[:24]
        self.assertEqual(rec["identity"]["pseudonym"], expect)

    # -- phone normalization --------------------------------------------
    def test_phone_formats(self):
        self.assertEqual(consent_intake.normalize_phone("0470123456"), "+32470123456")
        self.assertEqual(consent_intake.normalize_phone("+32 470 12 34 56"), "+32470123456")
        self.assertEqual(consent_intake.normalize_phone("0032470123456"), "+32470123456")
        with self.assertRaises(consent_intake.ValidationError):
            consent_intake.normalize_phone("abc")

    # -- validation ------------------------------------------------------
    def test_missing_required_rejected(self):
        p = _payload(answers=_answers(first_name=""))
        r = consent_intake.handler(_event(p), None)
        self.assertEqual(r["statusCode"], 422)
        self.assertEqual(json.loads(r["body"])["field"], "first_name")
        self.assertEqual(len(_FAKE_S3.puts), 0)

    def test_whatsapp_confirmation_required(self):
        r = consent_intake.handler(_event(_payload(answers=_answers(whatsapp_number_confirmed=False))), None)
        self.assertEqual(r["statusCode"], 422)
        self.assertEqual(json.loads(r["body"])["field"], "whatsapp_number_confirmed")
        self.assertEqual(len(_FAKE_S3.puts), 0)

    def test_bad_email_rejected(self):
        r = consent_intake.handler(_event(_payload(answers=_answers(email="nope"))), None)
        self.assertEqual(r["statusCode"], 422)
        self.assertEqual(json.loads(r["body"])["field"], "email")

    def test_slider_out_of_range_rejected(self):
        r = consent_intake.handler(_event(_payload(answers=_answers(confidence_self_improve=11))), None)
        self.assertEqual(r["statusCode"], 422)

    def test_help_areas_limits(self):
        r = consent_intake.handler(_event(_payload(answers=_answers(
            help_areas=["Gezonder eten", "Beter slapen", "Meer bewegen"]))), None)
        self.assertEqual(r["statusCode"], 422)

    def test_invalid_enum_rejected(self):
        r = consent_intake.handler(_event(_payload(answers=_answers(gender="xxx"))), None)
        self.assertEqual(r["statusCode"], 422)

    # -- consent gate (Art. 9) ------------------------------------------
    def test_missing_health_consent_refused(self):
        p = _payload(consents={"consent_health_data_processing": False})
        r = consent_intake.handler(_event(p), None)
        self.assertEqual(r["statusCode"], 422)
        self.assertEqual(json.loads(r["body"])["field"], "consent_health_data_processing")
        self.assertEqual(len(_FAKE_S3.puts), 0)

    # -- abuse guards ----------------------------------------------------
    def test_honeypot_accepts_but_stores_nothing(self):
        p = _payload(hp_field="i am a bot")
        r = consent_intake.handler(_event(p), None)
        self.assertEqual(r["statusCode"], 201)          # fake success
        self.assertEqual(len(_FAKE_S3.puts), 0)         # nothing written

    def test_echo_challenge_mismatch_rejected(self):
        p = _payload(challenge={"field": "first_name", "value": "WRONG"})
        r = consent_intake.handler(_event(p), None)
        self.assertEqual(r["statusCode"], 400)
        self.assertEqual(json.loads(r["body"])["error"], "challenge_mismatch")

    def test_echo_challenge_match_ok(self):
        p = _payload(challenge={"field": "first_name", "value": "jan"})   # case-insensitive
        r = consent_intake.handler(_event(p), None)
        self.assertEqual(r["statusCode"], 201)

    def test_oversized_payload_rejected(self):
        p = _payload(answers=_answers(notes_for_rudi="x" * 30000))
        r = consent_intake.handler(_event(p), None)
        self.assertEqual(r["statusCode"], 413)

    def test_options_preflight(self):
        r = consent_intake.handler(_event(None, method="OPTIONS"), None)
        self.assertEqual(r["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
