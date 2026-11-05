"""
MEET_RUDI — voice check: hear what a call voice really says, with nobody on the line.

Twilio owns speech on a call and never sends us audio, so the only honest way to check that a
voice speaks the language it is set up for is to record a real call. A voice check is one:

    dispatcher   places an ordinary ConversationRelay call (config `voice_check: true`) with
                 Twilio recording on, each side on its own channel — and only to one of the
                 account's OWN Twilio numbers. Any other destination is refused, so a person is
                 never recorded.
    check line   that number answers with ANSWER_TWIML (stay silent, then hang up), served by
                 meetrudi-call-status at ?answer=voice-check (dispatcher action
                 `voice_check_line` points the number there, or clears it)
    Rudi         speaks the call's welcome sample (fixed text in the call's language) and then
                 his normal opening, in the voice and language under test
    status       Twilio's recording callback: the WAV goes to calls/{id}/audio/recording.wav and
                 Twilio's copy is deleted
    report       Whisper (Groq) transcribes each channel WITHOUT being told the language and
                 names what it hears; a model then judges it against what Rudi was set up to say
                 → calls/{id}/voice_check.json

What is recorded is synthetic speech and silence. Run by services/call/rudi_voice_check.py.
"""

import array
import base64
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

import calllog
import gateway

TWILIO_REGION = os.environ.get("TWILIO_REGION", "ie1")
GROQ_SECRET = os.environ.get("GROQ_SECRET", "meetrudi-groq-firstkey")
ASR_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
ASR_MODEL = os.environ.get("GROQ_ASR_MODEL", "whisper-large-v3")

# The check line: pick up, stay silent while Rudi speaks, hang up. Long enough for the welcome
# sample and the opening (~15 s); short enough that a check costs seconds of airtime.
# A <Pause> FIRST would not pick up at all — Twilio waits that long before answering (seen
# 2026-11-05: the check ended "no-answer"). Half a second of DTMF silence answers the call.
ANSWER_TWIML = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<Response><Play digits="w"/><Pause length="22"/><Hangup/></Response>')

RECORDING_SID = re.compile(r"^RE[0-9a-f]{32}$")
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- Twilio
def _api(creds):
    return "https://api.%s.twilio.com/2010-04-01/Accounts/%s" % (TWILIO_REGION,
                                                                 creds["account_sid"])


def _twilio(method, url, creds, form=None):
    """Authenticated Twilio REST call. Returns (status, body bytes)."""
    data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
    token = base64.b64encode(
        ("%s:%s" % (creds["account_sid"], creds["auth_token"])).encode()).decode("ascii")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Basic " + token,
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError("Twilio %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300])
                           ) from None


def own_numbers(creds):
    """The account's own Twilio numbers: {phone_number: number record}."""
    _, raw = _twilio("GET", _api(creds) + "/IncomingPhoneNumbers.json?PageSize=50", creds)
    return {n["phone_number"]: n for n in json.loads(raw).get("incoming_phone_numbers") or []}


def is_own_number(number, creds):
    try:
        return bool(number) and number in own_numbers(creds)
    except Exception as e:  # noqa: BLE001 - cannot verify => refuse
        print("WARN voice check: could not list own numbers (%s)" % type(e).__name__)
        return False


def set_check_line(creds, answer_url, enable=True, number=""):
    """Point an own number (not the caller id) at the check line, or clear it."""
    numbers = own_numbers(creds)
    candidates = [n for p, n in numbers.items()
                  if p != creds.get("from_number") and (n.get("capabilities") or {}).get("voice")
                  and (not number or p == number)]
    if not candidates:
        return {"ok": False, "error": "no own voice number other than the caller id"}
    line = candidates[0]
    url = answer_url if enable else ""
    _twilio("POST", _api(creds) + "/IncomingPhoneNumbers/%s.json" % line["sid"], creds,
            {"VoiceUrl": url, "VoiceMethod": "POST"})
    print("AUDIT: voice check line %s -> %s" % (line["sid"], url or "(cleared)"))
    return {"ok": True, "number": line["phone_number"], "voice_url": url}


def call_debug(call_sid, creds):
    """Twilio's own view of one call: its state and the errors it logged. Read-only."""
    if not re.match(r"^CA[0-9a-f]{32}$", call_sid or ""):
        return {"ok": False, "error": "bad call sid"}
    _, raw = _twilio("GET", _api(creds) + "/Calls/%s.json" % call_sid, creds)
    call = json.loads(raw)
    _, raw = _twilio("GET", _api(creds) + "/Calls/%s/Notifications.json" % call_sid, creds)
    notes = json.loads(raw).get("notifications") or []
    return {"ok": True, "call": {k: call.get(k) for k in (
        "status", "direction", "duration", "start_time", "end_time", "answered_by",
        "price", "parent_call_sid")},
        "errors": [{"code": n.get("error_code"), "level": n.get("log"),
                    "text": (n.get("message_text") or "")[:300]} for n in notes]}


def line_debug(number, creds):
    """The check line's side: recent calls TO it in both Twilio regions, with their errors,
    and the account's recent alerts. Read-only."""
    if not re.match(r"^\+\d{8,15}$", number or ""):
        return {"ok": False, "error": "bad number"}
    out = {"ok": True, "calls": [], "alerts": []}
    for region in ("ie1", "us1"):
        host = "api.twilio.com" if region == "us1" else "api.%s.twilio.com" % region
        base = "https://%s/2010-04-01/Accounts/%s" % (host, creds["account_sid"])
        try:
            _, raw = _twilio("GET", base + "/Calls.json?PageSize=5&To=%s"
                             % urllib.parse.quote(number), creds)
        except Exception as e:  # noqa: BLE001
            out["calls"].append({"region": region, "error": str(e)[:200]})
            continue
        for c in json.loads(raw).get("calls") or []:
            _, raw = _twilio("GET", base + "/Calls/%s/Notifications.json" % c["sid"], creds)
            out["calls"].append({
                "region": region, "sid": c["sid"], "status": c.get("status"),
                "direction": c.get("direction"), "start": c.get("start_time"),
                "duration": c.get("duration"),
                "errors": [{"code": n.get("error_code"),
                            "text": urllib.parse.unquote_plus(n.get("message_text") or "")[:300]}
                           for n in json.loads(raw).get("notifications") or []]})
    for host in ("monitor.twilio.com", "monitor.ie1.twilio.com"):
        try:
            _, raw = _twilio("GET", "https://%s/v1/Alerts?PageSize=8" % host, creds)
            out["alerts"] += [{"host": host, "code": a.get("error_code"),
                               "at": a.get("date_created"), "sid": a.get("resource_sid"),
                               "text": urllib.parse.unquote_plus(a.get("alert_text") or "")[:300]}
                              for a in json.loads(raw).get("alerts") or []]
        except Exception as e:  # noqa: BLE001
            out["alerts"].append({"host": host, "error": str(e)[:200]})
    return out


def record_params(status_url, call_id):
    """Calls API fields that record a voice check, each side on its own channel."""
    return {"Record": "true", "RecordingChannels": "dual",
            "RecordingStatusCallback": "%s?call_id=%s&recording=1" % (status_url, call_id),
            "RecordingStatusCallbackEvent": "completed",
            "RecordingStatusCallbackMethod": "POST"}


def store_recording(manifest, form, creds):
    """Twilio's recording callback: keep the WAV in S3, delete Twilio's copy.

    The URL is built from the recording sid rather than taken from the callback, and the
    recording must belong to this call — our credentials never go to a host we did not choose.
    """
    sid = form.get("RecordingSid") or ""
    if not RECORDING_SID.match(sid):
        return {"ok": False, "error": "bad recording sid"}
    base = _api(creds) + "/Recordings/%s" % sid
    _, meta = _twilio("GET", base + ".json", creds)
    if json.loads(meta).get("call_sid") != (manifest.get("telephony") or {}).get("call_sid"):
        return {"ok": False, "error": "recording is not this call's"}
    _, audio = _twilio("GET", base + ".wav", creds)
    key = "%s/%s/audio/recording.wav" % (calllog.PREFIX, manifest["call_id"])
    calllog.s3.put_object(Bucket=calllog.DATA_BUCKET, Key=key, Body=audio,
                          ContentType="audio/wav")
    _twilio("DELETE", base + ".json", creds)
    manifest.setdefault("audio", {}).update({"recording": key, "bytes": len(audio),
                                             "twilio_copy": "deleted"})
    return {"ok": True, "key": key}


# --------------------------------------------------------------------------- audio
def split_channels(wav_bytes):
    """A dual-channel WAV → one mono WAV per channel (16-bit PCM, as Twilio records)."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit audio, got %d-byte samples" % width)
    samples = array.array("h", frames)
    out = []
    for c in range(channels):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as m:
            m.setnchannels(1)
            m.setsampwidth(2)
            m.setframerate(rate)
            m.writeframes(samples[c::channels].tobytes())
        out.append(buf.getvalue())
    return out


def loudness(wav_bytes):
    """Mean absolute amplitude: separates the speaking channel from the silent one."""
    with wave.open(io.BytesIO(wav_bytes)) as w:
        samples = array.array("h", w.readframes(w.getnframes()))
    return sum(abs(s) for s in samples) / max(1, len(samples))


def transcribe(wav_bytes):
    """Whisper, NOT told the language. Returns (text, language it heard)."""
    boundary = "----meetrudi" + uuid.uuid4().hex
    body = bytearray()
    for name, value in (("model", ASR_MODEL), ("response_format", "verbose_json"),
                        ("temperature", "0")):
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, name, value)).encode()
    body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"rudi.wav\"\r\n"
             "Content-Type: audio/wav\r\n\r\n" % boundary).encode()
    body += wav_bytes + ("\r\n--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(ASR_ENDPOINT, data=bytes(body), method="POST", headers={
        "Authorization": "Bearer " + gateway.get_secret(GROQ_SECRET),
        "Content-Type": "multipart/form-data; boundary=%s" % boundary, "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        out = json.loads(r.read().decode("utf-8"))
    return (out.get("text") or "").strip(), (out.get("language") or "").strip().lower()


JUDGE = """You check a phone voice for a text-to-speech test. A voice was set up to speak \
{language}. It was given the text under EXPECTED. Speech recognition, not told the language, \
transcribed what was actually heard on the line (HEARD) and identified the language as \
"{heard_language}".

EXPECTED:
{expected}

HEARD:
{heard}

Answer in JSON only: {{"language_ok": true/false (was the speech in {language}?), \
"text_ok": true/false (does HEARD say essentially the EXPECTED text, allowing for recognition \
errors in names and numbers?), "accent": "one short phrase on the accent or variety if the \
transcript shows one, else empty", "notes": "one sentence"}}"""


def _expected(manifest):
    config = manifest.get("config") or {}
    welcome = ((config.get("voice_attrs") or {}).get("welcomeGreeting") or "").strip()
    opening = (calllog._get_json("%s/%s/turns/0000.json" % (calllog.PREFIX,
                                                            manifest["call_id"])) or {})
    said = re.sub(r"<\s*\|\s*[A-Za-z0-9_\-]+\s*\|\s*>", config.get("user_name") or "them",
                  opening.get("reply") or "")
    return " ".join(p for p in (welcome, said) if p)


def report(call_id):
    """Transcribe and judge a finished voice check. Stored at calls/{id}/voice_check.json."""
    manifest = calllog.load(call_id) or {}
    if not (manifest.get("config") or {}).get("voice_check"):
        return {"ok": False, "error": "not a voice check"}
    # The fixed key, not manifest["audio"]: Twilio's status and recording callbacks can land
    # together, and either may write the manifest last.
    key = "%s/%s/audio/recording.wav" % (calllog.PREFIX, call_id)
    try:
        wav = calllog.s3.get_object(Bucket=calllog.DATA_BUCKET, Key=key)["Body"].read()
    except Exception:  # noqa: BLE001 - not there yet
        return {"ok": False, "error": "no recording yet", "status": manifest.get("status")}
    channels = split_channels(wav)
    loud = [loudness(c) for c in channels]
    rudi = max(range(len(channels)), key=lambda i: loud[i])
    channel_key = key.replace("recording.wav", "rudi.wav")
    calllog.s3.put_object(Bucket=calllog.DATA_BUCKET, Key=channel_key, Body=channels[rudi],
                          ContentType="audio/wav")

    started = time.time()
    heard, heard_language = transcribe(channels[rudi])
    config = manifest.get("config") or {}
    expected = _expected(manifest)
    verdict = {}
    try:
        result = gateway.generate([{"role": "user", "content": JUDGE.format(
            language=config.get("language"), expected=expected, heard=heard,
            heard_language=heard_language)}], json_mode=True)
        verdict = json.loads(result["text"])
    except Exception as e:  # noqa: BLE001 - the transcript and detected language still stand
        verdict = {"error": "judge unavailable (%s)" % type(e).__name__}
    out = {
        "ok": True, "call_id": call_id, "language": config.get("language"),
        "voice": (config.get("voice_attrs") or {}).get("voice")
        or (manifest.get("telephony") or {}).get("voice"),
        "provider": (config.get("voice_attrs") or {}).get("ttsProvider")
        or ((manifest.get("telephony") or {}).get("voice_key") or "").split(":")[0],
        "engine": manifest.get("engine"), "heard_language": heard_language, "heard": heard,
        "expected": expected, "verdict": verdict, "rudi_channel": rudi + 1,
        "loudness": [round(v) for v in loud], "rudi_audio": channel_key,
        "ms": int((time.time() - started) * 1000),
    }
    calllog._put_json("%s/%s/voice_check.json" % (calllog.PREFIX, call_id), out)
    return out
