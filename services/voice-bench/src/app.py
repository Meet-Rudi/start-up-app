"""
MEET_RUDI — meetrudi-voice-bench Lambda handler.

Phase-0 bench for the outbound goal-setting call. The browser is a microphone and a speaker;
this Lambda is the whole apparatus: transcribe -> understand -> generate -> speak, with every
stage timed and the entire call written to S3 for analysis.

Three actions, all POSTed as JSON to the Function URL:

  {"action":"start", "config":{...}}
      -> {"ok":true, "call_id":..., "reply":..., "audio_b64":..., "audio_mime":...,
          "phase":..., "timings":{...}}

  {"action":"turn", "call_id":..., "audio_b64":..., "audio_mime":"audio/webm;codecs=opus"}
      -> {"ok":true, "transcript":..., "reply":..., "audio_b64":..., "phase":...,
          "signals":{...}, "ended":bool, "timings":{"asr_ms","llm_ms","tts_ms","server_ms"}}

  {"action":"end", "call_id":..., "reason":"hangup"}
      -> {"ok":true, "manifest":{...}}

Call config (all optional except where noted; defaults applied in _clean_config):

  {"language":"en", "user_name":"Filip", "topic":"walking again after knee surgery",
   "voice":"troy", "start_phase":"goal", "max_minutes":12, "store_audio":true,
   "notes":"free-text extra context for Rudi"}
"""

import os
import re
import json
import time
import base64
import datetime

import brain
import speech
import calllog
import gateway

ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "https://meet-rudi.github.io")
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_BYTES", "8000000"))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "3000"))
MAX_FEEDBACK_CHARS = int(os.environ.get("MAX_FEEDBACK_CHARS", "4000"))

DEFAULT_CONFIG = {
    "language": "en",
    "user_name": "",
    "topic": "",
    # Provider-neutral: DEFAULT_VOICE is set per environment, so switching TTS_PROVIDER does not
    # leave a voice name from the previous provider baked into every call config.
    "voice": os.environ.get("DEFAULT_VOICE", "en_US-ryan-medium"),
    "start_phase": "goal",
    "max_minutes": 12,
    "store_audio": True,
    # Silence after each sentence. Piper leaves none, and the terminator scales this: a question
    # rests 1.5x as long, which is what hands the turn back to the patient.
    "sentence_gap_ms": int(os.environ.get("SENTENCE_GAP_MS", "300")),
    "notes": "",
}

FRIENDLY_ERROR = "Sorry, something went wrong on my side. Let's try that again."
# One message for every fault that ends the session, in the same words the call service speaks —
# a tester and a patient should meet the same Rudi. Which internal thing broke is our problem.
UNAVAILABLE_MESSAGE = ("I apologise, but for operational reasons I need to pause our discussion "
                       "here. I will come back to you soon. Have a great time in the meantime.")
TIRED_MESSAGE = UNAVAILABLE_MESSAGE



def _response(payload, status=200):
    """Note what is NOT here: Access-Control-Allow-Origin.

    The Function URL's own Cors config already emits it. Setting it here too made the response
    carry the header twice, and a browser rejects a duplicated Access-Control-Allow-Origin
    outright — which surfaces as a bare "Failed to fetch" with no useful detail. Server-side
    callers never saw it because they send no Origin and trigger no CORS check.
    """
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _meta(event):
    try:
        http = (event.get("requestContext") or {}).get("http", {})
        headers = event.get("headers") or {}
        return {"ip": http.get("sourceIp"), "user_agent": headers.get("user-agent")}
    except Exception:  # noqa: BLE001
        return {}


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _clean_config(supplied):
    """Merge caller config over defaults, coercing the fields we depend on."""
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(supplied, dict):
        for key, value in supplied.items():
            if key in cfg and value is not None:
                cfg[key] = value
            elif key not in cfg and value is not None:
                cfg[key] = value  # forward-compatible: unknown keys ride along into the record

    cfg["language"] = str(cfg.get("language") or "en").strip().lower()[:5] or "en"
    cfg["user_name"] = str(cfg.get("user_name") or "").strip()[:80]
    cfg["topic"] = str(cfg.get("topic") or "").strip()[:400]
    cfg["notes"] = str(cfg.get("notes") or "").strip()[:800]
    cfg["voice"] = str(cfg.get("voice") or DEFAULT_CONFIG["voice"]).strip()[:60]
    cfg["store_audio"] = bool(cfg.get("store_audio"))
    if str(cfg.get("start_phase")) not in ("learn", "goal", "commit"):
        cfg["start_phase"] = "goal"
    try:
        cfg["sentence_gap_ms"] = max(0, min(2000, int(cfg.get("sentence_gap_ms"))))
    except (TypeError, ValueError):
        cfg["sentence_gap_ms"] = DEFAULT_CONFIG["sentence_gap_ms"]
    # `or 12` would swallow an explicit 0, so test for None rather than falsiness.
    raw_minutes = cfg.get("max_minutes")
    try:
        cfg["max_minutes"] = 12 if raw_minutes is None else max(1, min(30, int(raw_minutes)))
    except (TypeError, ValueError):
        cfg["max_minutes"] = 12
    return cfg


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
# ~14 characters per second of speech, so 16 buys roughly 1.2s of lead — comfortably longer
# than the round trip that fetches the remainder.
LEAD_MIN_CHARS = int(os.environ.get("LEAD_MIN_CHARS", "16"))


def _split_lead(text):
    """Split a reply into (lead, rest) so the lead can start playing while `rest` renders.

    Synthesising a whole reply costs ~2s; the first sentence costs ~0.4s. Speaking the lead
    immediately and fetching the remainder during playback is what turns a 2s silence into a
    sub-second one — the seam lands inside audio the listener is already hearing.

    Very short openers ("Hi Filip.") are merged forward, because a 0.6s clip followed by a
    fetch is more audible as a stutter than it is useful as a head start.
    """
    parts = _SENTENCE_END.split((text or "").strip())
    if len(parts) < 2:
        return text, ""
    lead = parts[0]
    index = 1
    while index < len(parts) and len(lead) < LEAD_MIN_CHARS:
        lead += " " + parts[index]
        index += 1
    return lead, " ".join(parts[index:]).strip()


def _pace(params):
    """Clamp a client-supplied speaking pace.

    The bench measures how fast the patient talks and asks for a matching length_scale. Bounds
    are deliberately tight: Piper degrades audibly outside them, and mirroring someone exactly
    is uncanny rather than warm — the aim is to meet a slow speaker part of the way, not to
    impersonate their pace.
    """
    raw = params.get("length_scale")
    if raw is None:
        return None
    try:
        return max(0.85, min(1.45, float(raw)))
    except (TypeError, ValueError):
        return None


def _asr_hint(config):
    """Seed Whisper with the proper nouns it is most likely to mangle."""
    bits = [b for b in (config.get("user_name"), config.get("topic")) if b]
    return ("This is a call with Rudi, a health coaching assistant. " + " ".join(bits)).strip()


def _speak(text, config, length_scale=None):
    """Synthesise, but never let a TTS outage destroy the call.

    A failed voice still leaves a usable turn: the text reply, the phase transition and the S3
    record all survive, and the page falls back to showing what Rudi would have said. Returns
    (audio, mime, ms, error).
    """
    try:
        audio, mime, ms = speech.synthesize(text, voice=config.get("voice"),
                                            sentence_gap_ms=config.get("sentence_gap_ms"),
                                            length_scale=length_scale)
        return audio, mime, ms, None
    except speech.SpeechError as e:
        print("TTS FAILED (continuing without audio): %s" % e)
        return b"", "audio/wav", 0, str(e)


def _elapsed_s(manifest):
    try:
        started = datetime.datetime.fromisoformat(manifest["started_at"])
        now = datetime.datetime.now(datetime.timezone.utc)
        return int((now - started).total_seconds())
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------------- actions

def _do_start(params, event):
    t0 = time.time()
    config = _clean_config(params.get("config"))
    call_id = calllog.new_call_id()

    reply, state, info = brain.open_call(config)
    llm_ms = int((time.time() - t0) * 1000)

    lead, rest = _split_lead(reply)
    audio, audio_mime, tts_ms, tts_error = _speak(lead, config)

    manifest = calllog.start(call_id, config, state, _meta(event))
    audio_ref = None
    if config["store_audio"] and audio:
        audio_ref = calllog.put_audio(call_id, 0, "rudi", audio,
                                      speech.ext_for_mime(audio_mime), audio_mime)

    timings = {"asr_ms": 0, "llm_ms": llm_ms, "tts_ms": tts_ms,
               "server_ms": int((time.time() - t0) * 1000)}
    calllog.record_turn(manifest, 0, {
        "at": calllog.iso(),
        "kind": "opening",
        "transcript": None,
        "reply": reply,
        "signals": {},
        "phase": state["phase"],
        "state": state,
        "model": info.get("model"),
        "timings": timings,
        "tts_error": tts_error,
        "audio": {"rudi": audio_ref, "user": None},
    })

    return _response({
        "ok": True, "call_id": call_id, "reply": reply,
        "audio_b64": base64.b64encode(audio).decode("ascii") if audio else None,
        "audio_mime": audio_mime, "tts_error": tts_error, "rest_text": rest,
        "phase": state["phase"], "signals": {}, "ended": False,
        "config": config, "timings": timings,
    })


def _do_turn(params, event):
    t0 = time.time()
    call_id = (params.get("call_id") or "").strip()
    if not call_id:
        return _response({"ok": False, "error": "call_id is required"}, 400)

    manifest = calllog.load(call_id)
    if not manifest:
        return _response({"ok": False, "error": "unknown call_id: %s" % call_id}, 404)
    if manifest.get("status") == "completed":
        return _response({"ok": False, "error": "call already ended"}, 409)

    config = _clean_config(manifest.get("config"))
    state = manifest.get("state") or brain.new_state(config)
    seq = int(manifest.get("totals", {}).get("turns", 0)) + 1

    audio_b64 = params.get("audio_b64") or ""
    if not audio_b64:
        return _response({"ok": False, "error": "audio_b64 is required"}, 400)
    user_audio = base64.b64decode(audio_b64)
    if len(user_audio) > MAX_AUDIO_BYTES:
        return _response({"ok": False, "error": "audio too large"}, 413)
    user_mime = (params.get("audio_mime") or "audio/webm").strip()

    transcript, asr_ms = speech.transcribe(
        user_audio, mime=user_mime, language=config["language"], hint=_asr_hint(config))

    user_audio_ref = None
    if config["store_audio"]:
        user_audio_ref = calllog.put_audio(call_id, seq, "user", user_audio,
                                           speech.ext_for_mime(user_mime), user_mime)

    # Nothing intelligible came back — don't burn an LLM turn or a phase-machine counter on it.
    if not transcript:
        timings = {"asr_ms": asr_ms, "llm_ms": 0, "tts_ms": 0,
                   "server_ms": int((time.time() - t0) * 1000)}
        calllog.record_turn(manifest, seq, {
            "at": calllog.iso(), "kind": "empty", "transcript": "", "reply": None,
            "signals": {}, "phase": state.get("phase"), "state": state, "model": None,
            "timings": timings, "audio": {"user": user_audio_ref, "rudi": None},
        })
        return _response({"ok": True, "call_id": call_id, "transcript": "", "reply": None,
                          "empty": True, "phase": state.get("phase"), "signals": {},
                          "ended": False, "timings": timings})

    t_llm = time.time()
    reply, state, info = brain.turn(state, transcript, config, elapsed_s=_elapsed_s(manifest))
    llm_ms = int((time.time() - t_llm) * 1000)

    audio_out, audio_mime, tts_ms, tts_error = (b"", "audio/wav", 0, None)
    rudi_audio_ref = None
    lead, rest = _split_lead(reply)
    if reply:
        audio_out, audio_mime, tts_ms, tts_error = _speak(lead, config, _pace(params))
        if config["store_audio"] and audio_out:
            rudi_audio_ref = calllog.put_audio(call_id, seq, "rudi", audio_out,
                                               speech.ext_for_mime(audio_mime), audio_mime)

    timings = {"asr_ms": asr_ms, "llm_ms": llm_ms, "tts_ms": tts_ms,
               "server_ms": int((time.time() - t0) * 1000)}
    calllog.record_turn(manifest, seq, {
        "at": calllog.iso(), "kind": "turn", "transcript": transcript, "reply": reply,
        "signals": info.get("signals", {}), "phase": state["phase"], "state": state,
        "model": info.get("model"), "timings": timings, "tts_error": tts_error,
        # How the client heard this turn. Without it a call cannot be audited after the fact:
        # a cadence adaptation that silently never fires looks identical to one that fired and
        # chose not to move.
        "pace": {
            "voiced_s": params.get("voiced_s"),
            "cadence_cps": params.get("cadence_cps"),
            "silence_ms": params.get("silence_ms"),
            "length_scale": _pace(params),
            "barged": bool(params.get("barged")),
        },
        "audio": {"user": user_audio_ref, "rudi": rudi_audio_ref},
    })

    return _response({
        "ok": True, "call_id": call_id, "transcript": transcript, "reply": reply,
        "audio_b64": base64.b64encode(audio_out).decode("ascii") if audio_out else None,
        "audio_mime": audio_mime, "tts_error": tts_error, "rest_text": rest,
        "phase": state["phase"],
        "signals": info.get("signals", {}), "ended": bool(info.get("ended")),
        "timings": timings,
    })


def _do_speak(params, _event):
    """Synthesise the remainder of a reply while its lead is already playing.

    Kept as its own action rather than folded into `turn` so the client controls when it is
    fetched — it fires the moment playback starts, not after it finishes.
    """
    t0 = time.time()
    manifest = calllog.load((params.get("call_id") or "").strip())
    if not manifest:
        return _response({"ok": False, "error": "unknown call_id"}, 404)

    text = str(params.get("text") or "").strip()
    if not text:
        return _response({"ok": False, "error": "text is required"}, 400)

    config = _clean_config(manifest.get("config"))
    audio, mime, tts_ms, tts_error = _speak(text[:MAX_TEXT_CHARS], config, _pace(params))
    return _response({
        "ok": True,
        "audio_b64": base64.b64encode(audio).decode("ascii") if audio else None,
        "audio_mime": mime, "tts_error": tts_error,
        "timings": {"tts_ms": tts_ms, "server_ms": int((time.time() - t0) * 1000)},
    })


def _do_end(params, _event):
    call_id = (params.get("call_id") or "").strip()
    manifest = calllog.load(call_id)
    if not manifest:
        return _response({"ok": False, "error": "unknown call_id: %s" % call_id}, 404)
    if manifest.get("status") != "completed":
        manifest = calllog.finish(manifest, (params.get("reason") or "hangup")[:60])
    return _response({"ok": True, "manifest": manifest})


def _do_feedback(params, event):
    """Record a tester's free-text note against the call.

    Written after the call, not during it: whoever is leading the conversation has no attention
    spare for annotating it, so nothing here asks them to mark where something happened.

    Deliberately permissive — needs nothing but text and never fails the caller for a missing
    field. During a testing session the cost of losing an observation is far higher than the
    cost of a slightly ragged record.
    """
    manifest = calllog.load((params.get("call_id") or "").strip())
    if not manifest:
        return _response({"ok": False, "error": "unknown call_id"}, 404)

    text = str(params.get("text") or "").strip()
    if not text:
        return _response({"ok": False, "error": "text is required"}, 400)

    record = calllog.add_feedback(manifest, {
        "text": text[:MAX_FEEDBACK_CHARS],
        "tester": str(params.get("tester") or "").strip()[:60] or None,
        "call_status": manifest.get("status"),
        "meta": _meta(event),
    })
    return _response({"ok": True, "seq": record["seq"],
                      "count": len(manifest.get("feedback") or [])})


ACTIONS = {"start": _do_start, "turn": _do_turn, "end": _do_end, "speak": _do_speak,
           "feedback": _do_feedback}


def handler(event, context):
    try:
        params = _body(event)
        action = (params.get("action") or "turn").strip()
        fn = ACTIONS.get(action)
        if fn is None:
            return _response({"ok": False, "error": "unknown action: %s" % action}, 400)
        return fn(params, event)

    except gateway.AllRateLimited as e:
        print("RATE LIMITED: %s" % e)
        return _response({"ok": False, "error": "rate_limited", "reply": TIRED_MESSAGE})
    except gateway.AIError as e:
        # Every endpoint in the cascade failed. The detail is for us, not for whoever is
        # holding the microphone: on 2026-08-17 a tester was shown three stacked HTTP 404s
        # naming decommissioned models, which tells them nothing they can act on.
        print("ALL ENDPOINTS FAILED: %s" % e)
        return _response({"ok": False, "error": "ai_unavailable", "reply": UNAVAILABLE_MESSAGE})
    except speech.SpeechError as e:
        print("SPEECH ERROR: %s" % e)
        return _response({"ok": False, "error": "speech: %s" % e, "reply": FRIENDLY_ERROR})
    except Exception as e:  # noqa: BLE001 - the bench must never hard-fail mid-call
        print("ERROR: %s" % e)
        return _response({"ok": False, "error": str(e), "reply": FRIENDLY_ERROR})
