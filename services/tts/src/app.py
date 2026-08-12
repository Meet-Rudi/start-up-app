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
import re
import json
import time
import uuid
import wave
import base64
import threading
import collections

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

# Any well-formed Piper voice name is accepted too, so seeding a new voice into S3 is enough to
# use it — no redeploy. Short keys above stay as convenience aliases for callers.
VOICE_NAME_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}-[A-Za-z0-9_]+-(x_low|low|medium|high)$")

# Prosody. Piper concatenates its per-sentence chunks with no gap, so without this every voice
# runs full stops together. 300ms reads as a normal spoken beat.
SENTENCE_GAP_MS = int(os.environ.get("SENTENCE_GAP_MS", "300"))
MAX_PAUSE_MS = 5000

# Tolerant on purpose. If an LLM ever emits these it will eventually write "[pause: 400]" or
# "[ pause:400 ]", and a near-miss must not end up spoken aloud as the words "pause four
# hundred". STRAY_PAUSE_RE sweeps up anything that still looks like a marker afterwards.
PAUSE_RE = re.compile(r"\[\s*pause\s*[:=]\s*(\d{1,5})\s*(?:ms)?\s*\]", re.IGNORECASE)
STRAY_PAUSE_RE = re.compile(r"\[\s*pause[^\]]{0,20}\]", re.IGNORECASE)

# Automatic prosody: how long to rest after each sentence terminator, as a multiple of the base
# gap. A question needs longer than a statement — Rudi ends most turns on one, and the pause is
# what tells the listener it is their turn to speak.
#
# Only sentence boundaries are timed automatically. Splitting mid-sentence (at an em dash, say)
# would make Piper render each fragment with falling terminal intonation, which sounds MORE
# clipped, not less — so mid-sentence beats stay manual via [pause:N].
TERMINAL_SCALE = {".": 1.0, "!": 1.35, "?": 1.5, "…": 1.5, ":": 0.8, ";": 0.8}
PARAGRAPH_SCALE = 2.2
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…:;])(\s+)")

# Each loaded PiperVoice holds an ONNX session worth ~300 MB of RSS, so this cache MUST be
# bounded: an unbounded one climbed 515 MB -> 2048 MB over seven voices and killed the container
# with Runtime.OutOfMemory. Least-recently-used is evicted; re-loading costs ~2s from /tmp,
# which is the right trade against dying. Rudi speaks four languages, so a live container can
# legitimately want several voices — this is not a synthetic limit.
MAX_CACHED_VOICES = int(os.environ.get("MAX_CACHED_VOICES", "3"))

_voice_cache = collections.OrderedDict()   # voice name -> PiperVoice (LRU order)
_voice_lock = threading.Lock()
_token_cache = {}

# A Lambda Function URL response is capped at 6 MB and audio ships base64 (+33%), so anything
# past ~4.2 MB cannot be returned inline. Rather than fail, hand back a short-lived presigned
# S3 URL — the caller downloads it instead. Real Rudi turns are a few seconds and never come
# near this; long-form renders (whole monologues, samples) routinely do.
MAX_INLINE_AUDIO_BYTES = int(os.environ.get("MAX_INLINE_AUDIO_BYTES", "4200000"))
PRESIGN_TTL_S = int(os.environ.get("PRESIGN_TTL_S", "3600"))


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
    """Load once per container, keeping at most MAX_CACHED_VOICES resident."""
    with _voice_lock:
        voice = _voice_cache.get(voice_name)
        if voice is not None:
            _voice_cache.move_to_end(voice_name)     # mark as most recently used
            return voice, 0

        started = time.time()
        model_path, config_path = _ensure_local(voice_name)
        from piper import PiperVoice          # imported late so cold-start cost is measurable
        voice = PiperVoice.load(model_path, config_path=config_path, use_cuda=False)

        _voice_cache[voice_name] = voice
        while len(_voice_cache) > MAX_CACHED_VOICES:
            evicted, _ = _voice_cache.popitem(last=False)
            print("INFO: evicted voice %s to stay within %d cached"
                  % (evicted, MAX_CACHED_VOICES))
        return voice, int((time.time() - started) * 1000)


def _silence(sample_rate, ms):
    """16-bit mono silence."""
    return b"\x00\x00" * int(sample_rate * max(0, ms) / 1000)


def _segment(text, base_gap_ms):
    """Split into sentences and decide how long to rest after each one.

    Yields (sentence, trailing_gap_ms). The gap scales with the terminator — a question earns
    half again as long as a statement — and a paragraph break earns more than either. The last
    sentence yields a gap of 0 so a clip never ends in padding.

    This is the industrialised form of hand-placed beats: no markup in the text, no cooperation
    needed from whatever produced it.
    """
    parts = [p for p in SENTENCE_SPLIT_RE.split(text) if p]
    sentences = [p for p in parts if p.strip()]
    separators = [p for p in parts if not p.strip()]

    for i, sentence in enumerate(sentences):
        if i == len(sentences) - 1:
            yield sentence.strip(), 0
            continue
        terminator = sentence.strip()[-1:]
        scale = TERMINAL_SCALE.get(terminator, 1.0)
        # A blank line between sentences is a deliberate structural break; rest longer.
        if i < len(separators) and separators[i].count("\n") >= 2:
            scale = max(scale, PARAGRAPH_SCALE)
        yield sentence.strip(), int(base_gap_ms * scale)


def _synthesize(voice, text, length_scale=None, speaker_id=None, sentence_gap_ms=None):
    """Render text to a RIFF WAV, with breathing room between sentences.

    Piper yields one audio chunk per sentence and `synthesize_wav()` concatenates them with no
    gap at all, which is why unpunctuated-sounding run-ons happen: the voice lands on a full
    stop and starts the next sentence in the same breath. We iterate the chunks ourselves and
    insert real silence between them.

    Two controls:
      sentence_gap_ms  — silence after every sentence (default SENTENCE_GAP_MS)
      [pause:800]      — an inline marker for a deliberate beat, in milliseconds. Single
                         brackets on purpose: Piper reserves [[ ... ]] for raw phoneme blocks.
    """
    from piper import SynthesisConfig

    syn_config = None
    kwargs = {}
    if length_scale is not None:
        kwargs["length_scale"] = float(length_scale)
    # Multi-speaker models (nl_NL-mls has 52) need an index; single-speaker models ignore it.
    if speaker_id is not None:
        kwargs["speaker_id"] = int(speaker_id)
    if kwargs:
        syn_config = SynthesisConfig(**kwargs)

    gap_ms = SENTENCE_GAP_MS if sentence_gap_ms is None else max(0, int(sentence_gap_ms))
    rate = voice.config.sample_rate
    pieces = []
    after_explicit_pause = False

    # re.split with a capturing group interleaves text and pause values: [txt, ms, txt, ms, txt]
    for index, part in enumerate(PAUSE_RE.split(text)):
        if index % 2:                                   # captured milliseconds
            pieces.append(_silence(rate, min(int(part), MAX_PAUSE_MS)))
            after_explicit_pause = True
            continue
        part = STRAY_PAUSE_RE.sub(" ", part)            # never let a malformed marker be spoken
        if not part.strip():
            continue

        for sentence, trailing_gap in _segment(part, gap_ms):
            for chunk in voice.synthesize(sentence, syn_config=syn_config):
                rate = chunk.sample_rate
                if pieces and not after_explicit_pause:
                    pieces.append(_silence(rate, gap_ms))
                after_explicit_pause = False
                pieces.append(chunk.audio_int16_bytes)
            if trailing_gap:
                pieces.append(_silence(rate, trailing_gap))
                after_explicit_pause = True             # don't stack the base gap on top

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(b"".join(pieces))
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
        voice_name = VOICES.get(key) or (key if VOICE_NAME_RE.match(key) else None)
        if not voice_name:
            return _response({"ok": False, "error": "unknown voice %r; aliases are %s, or pass a "
                              "full Piper name like en_US-amy-medium"
                              % (key, sorted(VOICES))}, 400)

        voice, load_ms = _get_voice(voice_name)

        synth_started = time.time()
        audio = _synthesize(voice, text, params.get("length_scale"), params.get("speaker_id"),
                            params.get("sentence_gap_ms"))
        synth_ms = int((time.time() - synth_started) * 1000)

        oversized = len(audio) > MAX_INLINE_AUDIO_BYTES
        audio_b64, audio_url = None, None
        if oversized:
            key = "%s/tts-out/%s-%s.wav" % (VOICE_PREFIX, voice_name, uuid.uuid4().hex[:10])
            s3.put_object(Bucket=DATA_BUCKET, Key=key, Body=audio, ContentType="audio/wav")
            audio_url = s3.generate_presigned_url(
                "get_object", Params={"Bucket": DATA_BUCKET, "Key": key},
                ExpiresIn=PRESIGN_TTL_S)
            print("INFO: %d bytes exceeds the inline limit; served via presigned URL %s"
                  % (len(audio), key))
        else:
            audio_b64 = base64.b64encode(audio).decode("ascii")

        return _response({
            "ok": True,
            "audio_b64": audio_b64,
            "audio_url": audio_url,
            "bytes": len(audio),
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
