"""
MEET_RUDI — meetrudi-call WebSocket handler.

One Lambda, three API Gateway routes. Twilio holds the socket; we are invoked per message and
exit. Nothing lives in memory between messages — the call's state is in S3, which is what makes
a per-message Lambda viable at all and why no long-running process is needed.

    $connect     accept, nothing else. The socket carries no identity yet.
    $default     setup | prompt | interrupt | dtmf | error
    $disconnect  finalise the record

The conversation itself is brain.py, unchanged from the bench and from WhatsApp: the same
learn/goal/commit machine, the same guardrail file leading the same system prompt. This service
adds telephony, not intelligence.
"""

import os
import json
import time

import boto3

import brain
import calllog
import relay
import gateway

DATA_BUCKET = os.environ["DATA_BUCKET"]
WS_ENDPOINT = os.environ.get("WS_MANAGEMENT_ENDPOINT", "")

FRIENDLY_ERROR = "Sorry, I lost my train of thought there. Could you say that again?"
TIRED_MESSAGE = ("I'm having trouble thinking clearly right now. Let me call you back another "
                 "time. Sorry about that.")

_api = None


def _mgmt():
    global _api
    if _api is None:
        _api = boto3.client("apigatewaymanagementapi", endpoint_url=WS_ENDPOINT)
    return _api


def _send(connection_id, payload):
    """Push a frame to Twilio. A dead socket is normal — the patient hung up."""
    try:
        _mgmt().post_to_connection(ConnectionId=connection_id, Data=payload.encode("utf-8"))
        return True
    except Exception as e:  # noqa: BLE001
        print("INFO: could not send on %s (%s)" % (connection_id, type(e).__name__))
        return False


def _ok(body=""):
    return {"statusCode": 200, "body": body}


# --------------------------------------------------------------------------- session lookup

def _link_key(connection_id):
    """Twilio's setup message carries the call_id; later messages do not. This is the map."""
    return "%s/_ws/%s.json" % (calllog.PREFIX, connection_id)


def _remember(connection_id, call_id):
    calllog._put_json(_link_key(connection_id), {"call_id": call_id, "at": calllog.iso()})


def _recall(connection_id):
    link = calllog._get_json(_link_key(connection_id)) or {}
    return link.get("call_id")


# --------------------------------------------------------------------------- message handlers

def _on_setup(connection_id, message):
    """The patient answered. Speak first — this is an outbound call."""
    params = message.get("customParameters") or {}
    call_id = (params.get("call_id") or "").strip()
    manifest = calllog.load(call_id) if call_id else None
    if not manifest:
        print("ERROR: setup for unknown call_id %r; ending session" % call_id)
        _send(connection_id, relay.hang_up("unknown-call"))
        return _ok()

    _remember(connection_id, call_id)
    manifest.setdefault("telephony", {}).update({
        "call_sid": message.get("callSid"),
        "session_id": message.get("sessionId"),
        "from": message.get("from"),
        "direction": message.get("direction"),
        "answered_at": calllog.iso(),
    })

    config = manifest.get("config") or {}
    started = time.time()
    try:
        reply, state, info = brain.open_call(config)
    except gateway.AllRateLimited:
        _send(connection_id, relay.say(TIRED_MESSAGE))
        _send(connection_id, relay.hang_up("rate-limited"))
        return _ok()

    manifest["state"] = state
    _send(connection_id, relay.say(reply))

    calllog.record_turn(manifest, 0, {
        "at": calllog.iso(), "kind": "opening", "transcript": None, "reply": reply,
        "signals": {}, "phase": state["phase"], "state": state, "model": info.get("model"),
        "timings": {"llm_ms": int((time.time() - started) * 1000)},
    })
    return _ok()


def _on_prompt(connection_id, message, manifest):
    """One finished utterance from the patient."""
    started = time.time()
    transcript = (message.get("voicePrompt") or "").strip()
    if not transcript:
        return _ok()

    config = manifest.get("config") or {}
    state = manifest.get("state") or brain.new_state(config)
    seq = int(manifest.get("totals", {}).get("turns", 0)) + 1

    # No AMD in Belgium, so the first thing we hear is the only chance to notice an answerphone.
    if seq == 1 and relay.looks_like_voicemail(transcript):
        print("INFO: voicemail suspected on %s: %r" % (manifest["call_id"], transcript[:80]))
        manifest.setdefault("telephony", {})["voicemail_suspected"] = True
        calllog.record_turn(manifest, seq, {
            "at": calllog.iso(), "kind": "voicemail", "transcript": transcript, "reply": None,
            "signals": {}, "phase": state.get("phase"), "state": state, "model": None,
            "timings": {},
        })
        _send(connection_id, relay.hang_up("voicemail"))
        return _ok()

    elapsed = _elapsed_s(manifest)
    try:
        reply, state, info = brain.turn(state, transcript, config, elapsed_s=elapsed)
    except gateway.AllRateLimited:
        _send(connection_id, relay.say(TIRED_MESSAGE))
        _send(connection_id, relay.hang_up("rate-limited"))
        return _ok()
    except Exception as e:  # noqa: BLE001 — a dropped turn must not drop the call
        print("ERROR: turn failed: %s" % e)
        _send(connection_id, relay.say(FRIENDLY_ERROR))
        return _ok()

    if reply:
        _send(connection_id, relay.say(reply))

    calllog.record_turn(manifest, seq, {
        "at": calllog.iso(), "kind": "turn", "transcript": transcript, "reply": reply,
        "signals": info.get("signals", {}), "phase": state["phase"], "state": state,
        "model": info.get("model"),
        "timings": {"llm_ms": int((time.time() - started) * 1000)},
        "elapsed_s": elapsed,
    })

    if info.get("ended"):
        # Twilio speaks the queued token before acting on `end`, so the goodbye is not cut off.
        _send(connection_id, relay.hang_up("engine-ended"))
    return _ok()


def _on_interrupt(connection_id, message, manifest):
    """The patient talked over Rudi. Record it — being interrupted is a quality signal."""
    manifest.setdefault("interruptions", []).append({
        "at": calllog.iso(),
        "said_so_far": message.get("utteranceUntilInterrupt"),
        "after_ms": message.get("durationUntilInterruptMs"),
    })
    calllog._put_json(calllog._manifest_key(manifest["call_id"]), manifest)
    return _ok()


def _elapsed_s(manifest):
    try:
        import datetime
        started = datetime.datetime.fromisoformat(
            (manifest.get("telephony") or {}).get("answered_at") or manifest["started_at"])
        now = datetime.datetime.now(datetime.timezone.utc)
        return int((now - started).total_seconds())
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------- entry point

def handler(event, context):
    ctx = event.get("requestContext") or {}
    route = ctx.get("routeKey")
    connection_id = ctx.get("connectionId")

    if route == "$connect":
        return _ok()

    if route == "$disconnect":
        call_id = _recall(connection_id)
        manifest = calllog.load(call_id) if call_id else None
        if manifest and manifest.get("status") != "completed":
            calllog.finish(manifest, "disconnected")
        return _ok()

    message = relay.parse(event.get("body") or "{}")
    kind = message.get("type")

    try:
        if kind == "setup":
            return _on_setup(connection_id, message)

        call_id = _recall(connection_id)
        manifest = calllog.load(call_id) if call_id else None
        if not manifest:
            print("WARN: %s on a connection with no call (%s)" % (kind, connection_id))
            return _ok()

        if kind == "prompt":
            return _on_prompt(connection_id, message, manifest)
        if kind == "interrupt":
            return _on_interrupt(connection_id, message, manifest)
        if kind == "error":
            print("TWILIO ERROR: %s" % message.get("description"))
            return _ok()
        return _ok()

    except Exception as e:  # noqa: BLE001 — never let an exception drop a live call silently
        print("ERROR: unhandled %s on %s: %s" % (kind, connection_id, e))
        _send(connection_id, relay.say(FRIENDLY_ERROR))
        return _ok()
