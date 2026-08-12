"""
MEET_RUDI — meetrudi-tts Lambda handler.

Piper text-to-speech on CPU. No GPU, no per-character billing, no US processor: the model
weights sit in our own EU bucket and the synthesis happens inside the Lambda.

Why this exists as its own service rather than inside meetrudi-voice-bench: Piper needs a
136 MB native layer, and the bench is a fast, dependency-free zip deploy we want to keep
iterating on. Splitting them also makes the voice a swappable component for Plan A later.

Request (POST JSON to the Function URL):
    {"token": "...", "text": "Hallo Filip", "voice": "nl", "format": "wav",
     "length_scale": 1.0}

Response:
    {"ok": true, "audio_b64": "...", "mime": "audio/wav", "sample_rate": 22050,
     "voice": "nl_BE-nathalie-medium", "timings": {"load_ms": 0, "synth_ms": 210}}

Voices are short keys so callers never hardcode a filename. Note that `nl` resolves to the
BELGIAN Flemish voice, not the Netherlands one — the pilot cohort is Flemish, and a
Netherlands-Dutch voice reads as audibly foreign to them.
"""

import os
import io
import json
import time
import wave
import base64
import threading

import boto3

s3 = boto3.client("s3")
_secrets = boto3.client("secretsmanager")

DATA_BUCKET = os.environ["DATA_BUCKET"]
VOICE_PREFIX = os.environ.get("VOICE_PREFIX", "voices/piper")
AUTH_SECRET = os.environ.get("AUTH_SECRET", "meetrudi/tts/token")
CACHE_DIR = os.environ.get("VOICE_CACHE_DIR", "/tmp/voices")
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "3000"))

# Short key -> Piper voice name. `nl` is deliberately the Flemish voice.
VOICES = {
    "en": "en_US-lessac-medium",
    "en_US": "en_US-lessac-medium",
    "nl": "nl_BE-nathalie-medium",
    "nl_BE": "nl_BE-nathalie-medium",
    "nl_NL": "nl_NL-mls-medium",
    "fr": "fr_FR-siwis-medium",
    "de": "de_DE-thorsten-medium",
}
DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "en")

_voice_cache = {}          # voice name -> PiperVoice
_voice_lock = threading.Lock()
_token_cache = {}


class TtsError(Exception):
    """Synthesis could not be produced."""


def _response(payload, status=200):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _expected_token():
    """Shared secret guarding a public Function URL. Fails CLOSED when unset.

    A missing secret must degrade to "" so the caller gets the intended 503, not a raw AWS
    exception surfaced as a 500.
    """
    if "token" in _token_cache:
        return _token_cache["token"]
    try:
        raw = _secrets.get_secret_value(SecretId=AUTH_SECRET).get("SecretString", "") or ""
    except Exception as e:  # noqa: BLE001
        print("WARN: auth secret %s unavailable (%s) — failing closed" % (AUTH_SECRET, e))
        return ""
    token = raw.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            token = str(obj.get("token") or obj.get("secret") or "").strip()
    except (ValueError, TypeError):
        pass
    _token_cache["token"] = token
    return token


def _ensure_local(voice_name):
    """Pull the .onnx and its .json from S3 into /tmp once per container."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    model_path = os.path.join(CACHE_DIR, voice_name + ".onnx")
    config_path = model_path + ".json"

    for path, key in ((model_path, "%s/%s.onnx" % (VOICE_PREFIX, voice_name)),
                      (config_path, "%s/%s.onnx.json" % (VOICE_PREFIX, voice_name))):
        if os.path.exists(path) and os.path.getsize(path) > 0:
            continue
        try:
            s3.download_file(DATA_BUCKET, key, path)
        except Exception as e:  # noqa: BLE001
            raise TtsError("voice %r not in s3://%s/%s (%s) — run seed_voices.py"
                           % (voice_name, DATA_BUCKET, key, e))
    return model_path, config_path


def _get_voice(voice_name):
    """Load once per container; every later call in this container is free."""
    voice = _voice_cache.get(voice_name)
    if voice is not None:
        return voice, 0

    with _voice_lock:
        voice = _voice_cache.get(voice_name)
        if voice is not None:
            return voice, 0
        started = time.time()
        model_path, config_path = _ensure_local(voice_name)
        from piper import PiperVoice          # imported late so cold-start cost is measurable
        voice = PiperVoice.load(model_path, config_path=config_path, use_cuda=False)
        _voice_cache[voice_name] = voice
        return voice, int((time.time() - started) * 1000)


def _synthesize(voice, text, length_scale=None):
    """Piper writes a RIFF WAV straight into the buffer, so no re-encoding is needed."""
    from piper import SynthesisConfig

    syn_config = None
    if length_scale is not None:
        syn_config = SynthesisConfig(length_scale=float(length_scale))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file, syn_config=syn_config)
    return buf.getvalue()


def handler(event, context):
    started = time.time()
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        params = json.loads(raw) if raw.strip() else {}

        # Unauthenticated health check. Proves the native layer imports on this runtime, which
        # is the one thing that can silently break when wheels are built off-platform. It
        # synthesises nothing and reveals nothing, so it is safe to leave open.
        if params.get("ping"):
            try:
                import piper
                import onnxruntime
                return _response({"ok": True, "piper": getattr(piper, "__version__", "loaded"),
                                  "onnxruntime": onnxruntime.__version__,
                                  "voices": sorted(VOICES),
                                  "auth_configured": bool(_expected_token())})
            except Exception as e:  # noqa: BLE001
                return _response({"ok": False, "error": "layer import failed: %s" % e}, 500)

        expected = _expected_token()
        if not expected:
            return _response({"ok": False, "error": "auth not configured"}, 503)
        if (params.get("token") or "") != expected:
            return _response({"ok": False, "error": "unauthorized"}, 401)

        text = str(params.get("text") or "").strip()
        if not text:
            return _response({"ok": False, "error": "text is required"}, 400)
        text = text[:MAX_TEXT_CHARS]

        key = str(params.get("voice") or DEFAULT_VOICE).strip()
        voice_name = VOICES.get(key) or (key if key in VOICES.values() else None)
        if not voice_name:
            return _response({"ok": False, "error": "unknown voice %r; have %s"
                              % (key, sorted(VOICES))}, 400)

        voice, load_ms = _get_voice(voice_name)

        synth_started = time.time()
        audio = _synthesize(voice, text, params.get("length_scale"))
        synth_ms = int((time.time() - synth_started) * 1000)

        return _response({
            "ok": True,
            "audio_b64": base64.b64encode(audio).decode("ascii"),
            "mime": "audio/wav",
            "voice": voice_name,
            "sample_rate": voice.config.sample_rate,
            "chars": len(text),
            "timings": {"load_ms": load_ms, "synth_ms": synth_ms,
                        "total_ms": int((time.time() - started) * 1000)},
        })

    except TtsError as e:
        print("TTS ERROR: %s" % e)
        return _response({"ok": False, "error": str(e)}, 500)
    except Exception as e:  # noqa: BLE001
        print("ERROR: %s" % e)
        return _response({"ok": False, "error": str(e)}, 500)
