"""
meetrudi-tts tests — no network, no real Piper, no real Secrets Manager.

The Piper runtime only exists inside the Lambda layer (linux x86_64), so it is stubbed here;
what these tests actually guard is the contract around it: the auth gate, the voice-key
mapping, and the per-container caching.

Run:  python -m unittest discover -s services/tts/tests -v
"""

from __future__ import annotations

import os
import sys
import json
import types
import base64
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

BUCKET = "meetrudi-ai-data-test"
os.environ.setdefault("DATA_BUCKET", BUCKET)
os.environ.setdefault("VOICE_CACHE_DIR", os.path.join(HERE, ".tmp-voices"))

TOKEN = "test-token-abc123"
_LOADS: list = []


class _FakeSecrets:
    missing = False

    def get_secret_value(self, SecretId):  # noqa: N803
        if self.missing:
            raise RuntimeError("Secrets Manager can't find the specified secret.")
        return {"SecretString": json.dumps({"token": TOKEN})}


class _FakeS3:
    def download_file(self, Bucket, Key, Filename):  # noqa: N803
        os.makedirs(os.path.dirname(Filename), exist_ok=True)
        with open(Filename, "wb") as f:
            f.write(b"fake-onnx")


_FAKE_SECRETS = _FakeSecrets()
_FAKE_S3 = _FakeS3()

boto3_stub = types.ModuleType("boto3")
boto3_stub.client = lambda name, *a, **k: _FAKE_SECRETS if name == "secretsmanager" else _FAKE_S3
sys.modules["boto3"] = boto3_stub


# --- stub the Piper runtime that normally lives in the layer -------------------------------
class _FakeConfig:
    sample_rate = 22050


class _FakePiperVoice:
    def __init__(self, model_path):
        self.config = _FakeConfig()
        self.model_path = model_path

    @staticmethod
    def load(model_path, config_path=None, use_cuda=False):
        _LOADS.append(os.path.basename(str(model_path)))
        return _FakePiperVoice(model_path)

    def synthesize_wav(self, text, wav_file, syn_config=None):
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x01" * max(1, len(text)))


piper_stub = types.ModuleType("piper")
piper_stub.PiperVoice = _FakePiperVoice
piper_stub.SynthesisConfig = lambda **kw: kw
sys.modules["piper"] = piper_stub

# Only present inside the Lambda layer; the health check imports it to prove the layer loaded.
onnxruntime_stub = types.ModuleType("onnxruntime")
onnxruntime_stub.__version__ = "1.28.0-stub"
sys.modules["onnxruntime"] = onnxruntime_stub

import app  # noqa: E402


def _post(payload):
    ev = {"body": json.dumps(payload)}
    res = app.handler(ev, None)
    return res["statusCode"], json.loads(res["body"])


def _reset():
    app._token_cache.clear()
    app._voice_cache.clear()
    _LOADS.clear()
    _FAKE_SECRETS.missing = False


class Auth(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_missing_secret_fails_closed_with_503(self):
        """A missing secret must not surface as a raw AWS 500."""
        _FAKE_SECRETS.missing = True
        status, body = _post({"text": "hallo", "voice": "nl"})
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertIn("auth", body["error"])

    def test_wrong_token_rejected(self):
        status, body = _post({"token": "nope", "text": "hallo"})
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])

    def test_no_token_rejected(self):
        status, _ = _post({"text": "hallo"})
        self.assertEqual(status, 401)

    def test_ping_needs_no_token(self):
        status, body = _post({"ping": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["auth_configured"])

    def test_ping_reports_unconfigured_auth_without_crashing(self):
        _FAKE_SECRETS.missing = True
        status, body = _post({"ping": True})
        self.assertEqual(status, 200)
        self.assertFalse(body["auth_configured"])


class VoiceMapping(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_nl_resolves_to_belgian_flemish(self):
        """The pilot cohort is Flemish — plain `nl` must not give them a Netherlands voice."""
        _, body = _post({"token": TOKEN, "text": "Hallo Filip", "voice": "nl"})
        self.assertEqual(body["voice"], "nl_BE-nathalie-medium")

    def test_nl_nl_is_still_reachable(self):
        _, body = _post({"token": TOKEN, "text": "Hallo", "voice": "nl_NL"})
        self.assertEqual(body["voice"], "nl_NL-mls-medium")

    def test_all_four_required_languages_present(self):
        for key in ("en", "nl", "fr", "de"):
            _reset()
            status, body = _post({"token": TOKEN, "text": "test", "voice": key})
            self.assertEqual(status, 200, key)
            self.assertTrue(body["ok"], key)

    def test_full_voice_name_also_accepted(self):
        _, body = _post({"token": TOKEN, "text": "test", "voice": "de_DE-thorsten-medium"})
        self.assertEqual(body["voice"], "de_DE-thorsten-medium")

    def test_unknown_voice_is_a_400(self):
        status, body = _post({"token": TOKEN, "text": "test", "voice": "klingon"})
        self.assertEqual(status, 400)
        self.assertIn("unknown voice", body["error"])


class Synthesis(unittest.TestCase):

    def setUp(self):
        _reset()

    def test_returns_a_riff_wav(self):
        status, body = _post({"token": TOKEN, "text": "Hallo Filip", "voice": "nl"})
        self.assertEqual(status, 200)
        audio = base64.b64decode(body["audio_b64"])
        self.assertEqual(audio[:4], b"RIFF", "must be a playable WAV container")
        self.assertEqual(body["mime"], "audio/wav")
        self.assertEqual(body["sample_rate"], 22050)

    def test_empty_text_rejected(self):
        status, _ = _post({"token": TOKEN, "text": "   ", "voice": "nl"})
        self.assertEqual(status, 400)

    def test_text_is_truncated_not_rejected(self):
        _, body = _post({"token": TOKEN, "text": "a" * 99999, "voice": "en"})
        self.assertEqual(body["chars"], app.MAX_TEXT_CHARS)

    def test_voice_loads_once_per_container(self):
        """The 60MB model download and ONNX load must not repeat on every warm call."""
        _post({"token": TOKEN, "text": "one", "voice": "nl"})
        _post({"token": TOKEN, "text": "two", "voice": "nl"})
        _post({"token": TOKEN, "text": "three", "voice": "nl"})
        self.assertEqual(len(_LOADS), 1, "voice reloaded on a warm invocation")

    def test_load_time_reported_only_on_the_cold_call(self):
        _, first = _post({"token": TOKEN, "text": "one", "voice": "fr"})
        _, second = _post({"token": TOKEN, "text": "two", "voice": "fr"})
        self.assertGreaterEqual(first["timings"]["load_ms"], 0)
        self.assertEqual(second["timings"]["load_ms"], 0)

    def test_separate_voices_load_separately(self):
        _post({"token": TOKEN, "text": "hi", "voice": "en"})
        _post({"token": TOKEN, "text": "hallo", "voice": "nl"})
        self.assertEqual(len(_LOADS), 2)


if __name__ == "__main__":
    unittest.main()
