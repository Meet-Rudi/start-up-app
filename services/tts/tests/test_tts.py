"""
meetrudi-tts tests — no network, no real Piper, no real Secrets Manager.

The Piper runtime only exists inside the Lambda layer (linux x86_64), so it is stubbed here;
what these tests actually guard is the contract around it: the auth gate, the voice-key
mapping, and the per-container caching.

Run:  python -m unittest discover -s services/tts/tests -v
"""

from __future__ import annotations

import os
import re
import sys
import json
import wave
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

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        return {}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):  # noqa: N803
        return "https://example-bucket.s3.amazonaws.com/%s?signed=1" % Params["Key"]


_FAKE_SECRETS = _FakeSecrets()
_FAKE_S3 = _FakeS3()

boto3_stub = types.ModuleType("boto3")
boto3_stub.client = lambda name, *a, **k: _FAKE_SECRETS if name == "secretsmanager" else _FAKE_S3
sys.modules["boto3"] = boto3_stub


# --- stub the Piper runtime that normally lives in the layer -------------------------------
class _FakeConfig:
    sample_rate = 22050


class _FakeChunk:
    """Piper yields one of these per sentence."""

    def __init__(self, sentence):
        self.sample_rate = 22050
        # 100 frames per character, so durations are predictable in tests
        self.audio_int16_bytes = b"\x01\x01" * (100 * max(1, len(sentence)))


class _FakePiperVoice:
    def __init__(self, model_path):
        self.config = _FakeConfig()
        self.model_path = model_path
        self.last_syn_config = None

    @staticmethod
    def load(model_path, config_path=None, use_cuda=False):
        _LOADS.append(os.path.basename(str(model_path)))
        return _FakePiperVoice(model_path)

    def synthesize(self, text, syn_config=None):
        """Split on sentence-enders the way Piper does, one chunk each."""
        self.last_syn_config = syn_config
        for sentence in [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]:
            yield _FakeChunk(sentence)


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


class VoiceCacheBound(unittest.TestCase):
    """Each PiperVoice holds ~300MB of ONNX session. An unbounded cache OOM-killed the
    container after seven voices, which is how en_US-amy-medium first failed with a 502."""

    def setUp(self):
        _reset()

    def test_cache_never_exceeds_the_cap(self):
        for voice in ("en_US-lessac-medium", "nl_BE-nathalie-medium", "nl_NL-alex-medium",
                      "nl_NL-pim-medium", "en_GB-alan-medium", "en_US-amy-medium"):
            status, _ = _post({"token": TOKEN, "text": "test", "voice": voice})
            self.assertEqual(status, 200, voice)
            self.assertLessEqual(len(app._voice_cache), app.MAX_CACHED_VOICES)

    def test_least_recently_used_is_evicted_first(self):
        app.MAX_CACHED_VOICES = 2
        try:
            _post({"token": TOKEN, "text": "a", "voice": "en_US-lessac-medium"})
            _post({"token": TOKEN, "text": "b", "voice": "nl_BE-nathalie-medium"})
            _post({"token": TOKEN, "text": "c", "voice": "en_US-lessac-medium"})  # refresh lessac
            _post({"token": TOKEN, "text": "d", "voice": "nl_NL-pim-medium"})     # evicts nathalie
            self.assertIn("en_US-lessac-medium", app._voice_cache)
            self.assertNotIn("nl_BE-nathalie-medium", app._voice_cache)
        finally:
            app.MAX_CACHED_VOICES = 3

    def test_evicted_voice_reloads_rather_than_erroring(self):
        app.MAX_CACHED_VOICES = 1
        try:
            _post({"token": TOKEN, "text": "a", "voice": "en_US-lessac-medium"})
            _post({"token": TOKEN, "text": "b", "voice": "nl_BE-nathalie-medium"})
            status, body = _post({"token": TOKEN, "text": "c", "voice": "en_US-lessac-medium"})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertGreater(body["timings"]["load_ms"], -1, "reload should be reported")
            self.assertEqual(_LOADS.count("en_US-lessac-medium.onnx"), 2)
        finally:
            app.MAX_CACHED_VOICES = 3


def _frames(body):
    """Decode the returned WAV and count 16-bit mono frames."""
    import io
    with wave.open(io.BytesIO(base64.b64decode(body["audio_b64"])), "rb") as w:
        return w.getnframes(), w.getframerate()


class Prosody(unittest.TestCase):
    """Piper concatenates its per-sentence chunks with zero gap, which is why full stops ran
    together. Silence is inserted by us, so it needs guarding."""

    def setUp(self):
        _reset()

    def test_sentences_are_separated_by_silence(self):
        one = _post({"token": TOKEN, "text": "Hallo.", "voice": "nl",
                     "sentence_gap_ms": 0})[1]
        two = _post({"token": TOKEN, "text": "Hallo. Hallo.", "voice": "nl",
                     "sentence_gap_ms": 0})[1]
        gapped = _post({"token": TOKEN, "text": "Hallo. Hallo.", "voice": "nl",
                        "sentence_gap_ms": 500})[1]

        f_two, rate = _frames(two)
        f_gapped, _ = _frames(gapped)
        self.assertEqual(f_gapped - f_two, int(rate * 0.5),
                         "a 500ms gap must add exactly 500ms of frames")
        self.assertGreater(f_two, _frames(one)[0])

    def test_no_trailing_or_leading_silence(self):
        """The gap goes between sentences, never before the first or after the last."""
        single_a = _frames(_post({"token": TOKEN, "text": "Hallo.", "voice": "nl",
                                  "sentence_gap_ms": 0})[1])[0]
        single_b = _frames(_post({"token": TOKEN, "text": "Hallo.", "voice": "nl",
                                  "sentence_gap_ms": 900})[1])[0]
        self.assertEqual(single_a, single_b, "a one-sentence clip must not gain padding")

    def test_explicit_pause_marker_inserts_exact_silence(self):
        plain = _frames(_post({"token": TOKEN, "text": "Ja. Nee.", "voice": "nl",
                               "sentence_gap_ms": 0})[1])
        paused = _frames(_post({"token": TOKEN, "text": "Ja.[pause:750]Nee.", "voice": "nl",
                                "sentence_gap_ms": 0})[1])
        self.assertEqual(paused[0] - plain[0], int(paused[1] * 0.75))

    def test_pause_marker_is_not_spoken(self):
        """The marker must be consumed, not read out as text."""
        with_marker = _frames(_post({"token": TOKEN, "text": "Ja.[pause:0]Nee.", "voice": "nl",
                                     "sentence_gap_ms": 0})[1])[0]
        without = _frames(_post({"token": TOKEN, "text": "Ja. Nee.", "voice": "nl",
                                 "sentence_gap_ms": 0})[1])[0]
        self.assertEqual(with_marker, without)

    def test_explicit_pause_suppresses_the_automatic_gap(self):
        """An explicit beat replaces the sentence gap rather than stacking on top of it."""
        body = _post({"token": TOKEN, "text": "Ja.[pause:400]Nee.", "voice": "nl",
                      "sentence_gap_ms": 300})[1]
        baseline = _frames(_post({"token": TOKEN, "text": "Ja. Nee.", "voice": "nl",
                                  "sentence_gap_ms": 0})[1])
        self.assertEqual(_frames(body)[0] - baseline[0], int(baseline[1] * 0.4))

    def test_pause_is_capped(self):
        body = _post({"token": TOKEN, "text": "Ja.[pause:99999]Nee.", "voice": "nl",
                      "sentence_gap_ms": 0})[1]
        baseline = _frames(_post({"token": TOKEN, "text": "Ja. Nee.", "voice": "nl",
                                  "sentence_gap_ms": 0})[1])
        self.assertEqual(_frames(body)[0] - baseline[0], int(baseline[1] * app.MAX_PAUSE_MS / 1000))

    def test_length_scale_reaches_the_model(self):
        _post({"token": TOKEN, "text": "Hallo.", "voice": "nl", "length_scale": 1.25})
        cfg = app._voice_cache["nl_BE-nathalie-medium"].last_syn_config
        self.assertEqual(cfg["length_scale"], 1.25)

    def test_speaker_id_reaches_the_model(self):
        _post({"token": TOKEN, "text": "Hallo.", "voice": "nl_NL", "speaker_id": 7})
        cfg = app._voice_cache["nl_NL-mls-medium"].last_syn_config
        self.assertEqual(cfg["speaker_id"], 7)


class AutomaticProsody(unittest.TestCase):
    """Punctuation-driven timing — the industrialised form of hand-placed beats. No markup in
    the text, so nothing upstream has to cooperate."""

    def setUp(self):
        _reset()

    def _dur(self, text, gap=300):
        body = _post({"token": TOKEN, "text": text, "voice": "nl", "sentence_gap_ms": gap})[1]
        frames, rate = _frames(body)
        return frames / float(rate)

    def test_question_rests_longer_than_a_statement(self):
        """Rudi ends most turns on a question; the pause is what hands the turn over."""
        statement = self._dur("Ja zeker. Nee.")
        question = self._dur("Ja zeker? Nee.")
        self.assertGreater(question, statement)
        self.assertAlmostEqual(question - statement, 0.3 * (1.5 - 1.0), places=2)

    def test_exclamation_rests_longer_than_a_statement(self):
        self.assertGreater(self._dur("Ja zeker! Nee."), self._dur("Ja zeker. Nee."))

    def test_colon_rests_shorter_than_a_full_stop(self):
        self.assertLess(self._dur("Ja zeker: Nee."), self._dur("Ja zeker. Nee."))

    def test_paragraph_break_rests_longest(self):
        inline = self._dur("Ja zeker. Nee.")
        para = self._dur("Ja zeker.\n\nNee.")
        self.assertAlmostEqual(para - inline, 0.3 * (2.2 - 1.0), places=2)

    def test_clip_never_ends_in_padding(self):
        self.assertAlmostEqual(self._dur("Ja zeker?", gap=900),
                               self._dur("Ja zeker?", gap=0), places=3)


class MalformedMarkers(unittest.TestCase):
    """If an LLM is ever allowed to emit [pause:N] it will eventually typo one. A near-miss
    must be swallowed, never read aloud as the words 'pause four hundred'."""

    def setUp(self):
        _reset()

    def _frames_of(self, text):
        return _frames(_post({"token": TOKEN, "text": text, "voice": "nl",
                              "sentence_gap_ms": 0})[1])[0]

    def test_spaced_variants_are_still_honoured(self):
        baseline = self._frames_of("Ja. Nee.")
        for variant in ("Ja.[pause: 500]Nee.", "Ja.[ pause:500 ]Nee.",
                        "Ja.[PAUSE=500]Nee.", "Ja.[pause:500ms]Nee."):
            self.assertEqual(self._frames_of(variant) - baseline, int(22050 * 0.5), variant)

    def test_unparseable_marker_is_stripped_not_spoken(self):
        baseline = self._frames_of("Ja. Nee.")
        for junk in ("Ja.[pause]Nee.", "Ja.[pause:abc]Nee.", "Ja.[pause:]Nee."):
            self.assertEqual(self._frames_of(junk), baseline,
                             "%r changed the audio — it may be being spoken" % junk)


class ResponseSize(unittest.TestCase):
    """A Function URL response caps at 6MB and audio ships base64 (+33%). Oversized synthesis
    must still succeed — via a presigned URL — rather than 413 the caller."""

    def setUp(self):
        _reset()

    def test_oversized_audio_falls_back_to_a_presigned_url(self):
        original = app.MAX_INLINE_AUDIO_BYTES
        app.MAX_INLINE_AUDIO_BYTES = 100
        try:
            status, body = _post({"token": TOKEN, "text": "x" * 500, "voice": "en"})
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertIsNone(body["audio_b64"])
            self.assertTrue(body["audio_url"].startswith("https://"))
            self.assertGreater(body["bytes"], 100)
        finally:
            app.MAX_INLINE_AUDIO_BYTES = original

    def test_normal_audio_stays_inline(self):
        _, body = _post({"token": TOKEN, "text": "Kort.", "voice": "en"})
        self.assertIsNotNone(body["audio_b64"])
        self.assertIsNone(body["audio_url"])


if __name__ == "__main__":
    unittest.main()
