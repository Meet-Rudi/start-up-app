"""
MEET_RUDI — speech I/O for meetrudi-voice-bench.

Two calls, both against Groq's OpenAI-compatible audio API, both timed so the bench can show
where the seconds actually go:

  transcribe(audio, mime, language)  -> (text, ms)   whisper-large-v3-turbo
  synthesize(text, voice, fmt)       -> (audio, mime, ms)

Pure stdlib + the shared gateway (for Secrets Manager key retrieval), so the Lambda still ships
with zero pip dependencies. Multipart bodies are hand-rolled below for the same reason.

Swapping either provider means editing only this file — the rest of the service talks in
(bytes, mime) and never learns who produced them.
"""

import os
import json
import time
import uuid
import base64
import urllib.request
import urllib.error

import gateway

GROQ_SECRET = os.environ.get("GROQ_SECRET", "meetrudi-groq-firstkey")

ASR_ENDPOINT = os.environ.get(
    "GROQ_ASR_ENDPOINT", "https://api.groq.com/openai/v1/audio/transcriptions")
ASR_MODEL = os.environ.get("GROQ_ASR_MODEL", "whisper-large-v3-turbo")

TTS_ENDPOINT = os.environ.get(
    "GROQ_TTS_ENDPOINT", "https://api.groq.com/openai/v1/audio/speech")
TTS_MODEL = os.environ.get("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
TTS_VOICE = os.environ.get("GROQ_TTS_VOICE", "troy")
TTS_FORMAT = os.environ.get("GROQ_TTS_FORMAT", "wav")

# "piper" routes synthesis to meetrudi-tts (self-hosted, EU, speaks Dutch incl. Flemish);
# "groq" uses the hosted Orpheus voice. Everything else in the service is unaffected.
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "groq").strip().lower()
PIPER_URL = os.environ.get("PIPER_TTS_URL", "")
PIPER_SECRET = os.environ.get("PIPER_TTS_SECRET", "meetrudi/tts/token")

# Groq blocks urllib's default User-Agent (Cloudflare), same as the chat gateway.
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

_EXT_BY_MIME = {
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav",
    "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/flac": "flac",
}
_MIME_BY_FORMAT = {
    "wav": "audio/wav", "mp3": "audio/mpeg", "opus": "audio/ogg",
    "flac": "audio/flac", "aac": "audio/aac",
}


class SpeechError(Exception):
    """ASR or TTS could not produce a result."""


def ext_for_mime(mime):
    """File extension for a browser-supplied mime type (codec suffix stripped)."""
    base = (mime or "").split(";")[0].strip().lower()
    return _EXT_BY_MIME.get(base, "webm")


def _multipart(fields, file_field, filename, file_mime, file_bytes):
    """Build a multipart/form-data body. Returns (content_type, body)."""
    boundary = "----meetrudi" + uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        if value is None:
            continue
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                % (boundary, key, value)).encode("utf-8")
    out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
            "Content-Type: %s\r\n\r\n" % (boundary, file_field, filename, file_mime)).encode("utf-8")
    out += file_bytes
    out += ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    return "multipart/form-data; boundary=%s" % boundary, bytes(out)


def _post(url, headers, body, timeout):
    merged = dict(headers)
    merged.setdefault("User-Agent", _UA)
    req = urllib.request.Request(url, data=body, headers=merged, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise SpeechError("HTTP %s from %s: %s" % (e.code, url, detail))
    except urllib.error.URLError as e:
        raise SpeechError("Network error calling %s: %s" % (url, e.reason))


def transcribe(audio_bytes, mime="audio/webm", language="en", hint=""):
    """Speech -> text. Returns (transcript, elapsed_ms).

    `language` is an ISO-639-1 code and measurably improves accuracy, so the call config always
    carries one. `hint` is passed as Whisper's prompt: seeding it with the caller's name and the
    call topic markedly improves proper-noun accuracy, which is exactly where ASR fails us.
    """
    if not audio_bytes:
        raise SpeechError("No audio supplied to transcribe().")

    key = gateway.get_secret(GROQ_SECRET)
    fields = {
        "model": ASR_MODEL,
        "response_format": "json",
        "temperature": "0",
    }
    if language:
        fields["language"] = language
    if hint:
        fields["prompt"] = hint[:800]

    content_type, body = _multipart(
        fields, "file", "turn." + ext_for_mime(mime), mime or "audio/webm", audio_bytes)

    started = time.time()
    raw, _ = _post(ASR_ENDPOINT,
                   {"Authorization": "Bearer " + key, "Content-Type": content_type},
                   body, timeout=30)
    elapsed = int((time.time() - started) * 1000)

    try:
        text = (json.loads(raw.decode("utf-8")) or {}).get("text", "")
    except (ValueError, TypeError) as e:
        raise SpeechError("Unparseable ASR response: %s" % e)
    return (text or "").strip(), elapsed


def _synthesize_piper(text, voice, started, sentence_gap_ms=None):
    """Route to meetrudi-tts. Accepts short keys (en, nl, fr, de) or full Piper voice names."""
    if not PIPER_URL:
        raise SpeechError("TTS_PROVIDER=piper but PIPER_TTS_URL is unset.")
    token = gateway.get_secret(PIPER_SECRET)
    body = {"token": token, "text": text, "voice": voice}
    if sentence_gap_ms is not None:
        body["sentence_gap_ms"] = sentence_gap_ms
    payload = json.dumps(body).encode("utf-8")

    raw, _ = _post(PIPER_URL, {"Content-Type": "application/json"}, payload, timeout=60)
    elapsed = int((time.time() - started) * 1000)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError) as e:
        raise SpeechError("Unparseable TTS response: %s" % e)
    if not body.get("ok"):
        raise SpeechError("piper: %s" % body.get("error", "unknown"))
    return base64.b64decode(body["audio_b64"]), body.get("mime", "audio/wav"), elapsed


def synthesize(text, voice=None, fmt=None, model=None, sentence_gap_ms=None):
    """Text -> speech. Returns (audio_bytes, mime, elapsed_ms)."""
    if not (text or "").strip():
        raise SpeechError("No text supplied to synthesize().")

    if TTS_PROVIDER == "piper":
        return _synthesize_piper(text, voice or os.environ.get("DEFAULT_VOICE", "en"),
                                 time.time(), sentence_gap_ms)

    voice = voice or TTS_VOICE
    fmt = (fmt or TTS_FORMAT).lower()
    key = gateway.get_secret(GROQ_SECRET)
    payload = json.dumps({
        "model": model or TTS_MODEL,
        "voice": voice,
        "input": text,
        "response_format": fmt,
    }).encode("utf-8")

    started = time.time()
    audio, _ = _post(TTS_ENDPOINT,
                     {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                     payload, timeout=45)
    elapsed = int((time.time() - started) * 1000)

    if not audio:
        raise SpeechError("TTS returned an empty body.")
    return audio, _MIME_BY_FORMAT.get(fmt, "audio/wav"), elapsed
