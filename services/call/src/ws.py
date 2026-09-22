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
import deid
import relay
import gateway

DATA_BUCKET = os.environ["DATA_BUCKET"]
WS_ENDPOINT = os.environ.get("WS_MANAGEMENT_ENDPOINT", "")

# Recoverable: one turn failed but the call is fine. Rudi asks them to repeat and carries on.
# One per language for the same reason as the pause line below: an English sentence dropped into
# the middle of a Flemish call is its own small failure.
LOST_THREAD = {
    "en": "Sorry, I lost my train of thought there. Could you say that again?",
    "nl": "Sorry, ik was de draad even kwijt. Kun je dat nog eens zeggen?",
    "fr": "Désolé, j'ai perdu le fil. Pouvez-vous répéter ?",
    "de": "Entschuldigung, ich habe kurz den Faden verloren. Können Sie das wiederholen?",
}

# NOT recoverable: the call has to end. One message for every such fault — a patient should not
# get a different apology depending on which internal thing broke, and "the model is rate
# limited" is not their problem. Spoken, never silent: dead air followed by the line dropping
# reads as being hung up on.
#
# Static text, so it cannot be translated by the model that just failed — hence one per
# language. Falling back to English at the moment a Flemish speaker's call collapses would make
# a bad moment worse.
OPERATIONAL_PAUSE = {
    "en": ("I apologise, but for operational reasons I need to pause our discussion here. "
           "I will come back to you soon. Have a great time in the meantime."),
    "nl": ("Het spijt me, maar om operationele redenen moet ik ons gesprek hier even pauzeren. "
           "Ik kom binnenkort bij je terug. Nog een fijne dag verder."),
    "fr": ("Je suis désolé, mais pour des raisons opérationnelles je dois interrompre notre "
           "conversation ici. Je reviendrai vers vous bientôt. Bonne journée en attendant."),
    "de": ("Es tut mir leid, aber aus betrieblichen Gründen muss ich unser Gespräch hier "
           "unterbrechen. Ich melde mich bald wieder. Ihnen noch eine gute Zeit."),
}


def operational_pause(config):
    """The one thing Rudi says whenever a call has to end for our reasons rather than theirs."""
    lang = str((config or {}).get("language") or "en").strip().lower()[:2]
    return OPERATIONAL_PAUSE.get(lang, OPERATIONAL_PAUSE["en"])


def lost_thread(config):
    """What Rudi says when ONE turn failed and the conversation can still carry on."""
    lang = str((config or {}).get("language") or "en").strip().lower()[:2]
    return LOST_THREAD.get(lang, LOST_THREAD["en"])


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

# De-identification, on the live path.
#
# Two tiers, both pure regex — microseconds, not milliseconds. That matters here in a way it does
# not on WhatsApp: a call is a real-time conversation and anything that costs latency gets felt.
# No model is involved in the scrubbing.
#
#   redact()  irreversible. National number, email, phone, IBAN, card. Gone before the text is
#             written anywhere or shown to anyone, and never recoverable.
#   vault     reversible, SESSION-SCOPED. Third-party names become placeholders for the model,
#             and are swapped back only in the final breath before Twilio speaks them, so Rudi
#             still sounds natural saying "your daughter Anna".
#
# The vault is deliberately NOT persisted. It lives in the manifest only while the call is open
# and is destroyed at the end, so no name-to-alias mapping survives the conversation that
# produced it. That is the whole point: the alias is useless later, which is what makes storing
# the transcript safe.
_detector = deid.HeuristicDetector()


def _vault_for(manifest):
    return deid.AliasVault.from_dict((manifest.get("_vault") or None))


def _scrub_inbound(manifest, text, locale="en"):
    """Everything the patient said, cleaned before it is stored OR sent to a model."""
    clean, found = deid.redact(text or "")
    vault = _vault_for(manifest)
    masked = vault.mask(clean, _detector, locale)
    manifest["_vault"] = vault.to_dict()
    if found:
        counts = manifest.setdefault("pii", {}).setdefault("redacted", {})
        for label, n in found.items():
            counts[label] = counts.get(label, 0) + n
        print("PII: redacted %s on %s" % (sorted(found), manifest["call_id"]))
    return masked


def _restore_outbound(manifest, text):
    """Placeholders back to real names, in the last breath before Twilio speaks."""
    return _vault_for(manifest).unmask(text or "", fallback="them")


def _model_config(manifest, config):
    """The call config as the MODEL may see it (§0.1).

    The person's own name becomes an alias from the same vault that masks what they say, so Rudi
    still greets them by name — restored only in the last breath before Twilio speaks. The
    free-text topic and notes are scrubbed like any utterance. The stored config is untouched.
    """
    vault = _vault_for(manifest)
    safe = dict(config)
    name = (config.get("user_name") or "").strip()
    if name:
        safe["user_name"] = deid.PLACEHOLDER_FMT % vault.alias_for(name)
    manifest["_vault"] = vault.to_dict()
    lang = config.get("language", "en")
    for key in ("topic", "notes"):
        if config.get(key):
            safe[key] = _scrub_inbound(manifest, config[key], lang)
    return safe


def _forget_vault(manifest):
    """Destroy the name-to-alias map when the call ends. Nothing identifying outlives the call."""
    if manifest.pop("_vault", None) is not None:
        print("PII: vault discarded for %s" % manifest["call_id"])


def _abandon(connection_id, manifest, config, error, reason):
    """End a call that cannot continue, gracefully and on the record.

    Says the operational-pause line, hangs up, and finalises with the true reason so the failure
    is countable afterwards. AllRateLimited subclasses AIError, so one except clause covers both
    and they differ only in what gets recorded — never in what the patient hears.
    """
    print("CALL ABANDONED (%s): %s" % (reason, error))
    _forget_vault(manifest)
    _send(connection_id, relay.say(operational_pause(config)))
    _send(connection_id, relay.hang_up(reason))
    manifest.setdefault("telephony", {})["error"] = reason
    calllog.finish(manifest, reason)
    return _ok()


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

    # A speak-only call: one line in Rudi's own voice, then hang up. Placed when there was no AI
    # headroom to hold a conversation, so it must not touch the model — but it rides the normal
    # ConversationRelay TwiML precisely so the voice is the one this person already knows.
    # Twilio speaks a queued token before acting on `end`, which is what lets the line finish;
    # the same mechanism carries the goodbye in _abandon.
    line = (config.get("speak_only") or "").strip()
    if line:
        if _send(connection_id, relay.say(line)):
            vkey = (manifest.get("telephony") or {}).get("voice_key")
            if vkey and calllog.voice_health().get(vkey, {}).get("ok") is not True:
                calllog.mark_voice(vkey, True)
        _send(connection_id, relay.hang_up("speak-only"))
        _forget_vault(manifest)
        calllog.finish(manifest, "speak-only")
        print("CALL %s spoke the fallback line and hung up" % manifest["call_id"])
        return _ok()

    started = time.time()
    try:
        reply, state, info = brain.open_call(_model_config(manifest, config))
    except gateway.AIError as e:
        return _abandon(connection_id, manifest, config, e,
                        "rate-limited" if isinstance(e, gateway.AllRateLimited)
                        else "ai-unavailable")

    manifest["state"] = state
    if _send(connection_id, relay.say(_restore_outbound(manifest, reply))):
        vkey = (manifest.get("telephony") or {}).get("voice_key")
        if vkey and calllog.voice_health().get(vkey, {}).get("ok") is not True:
            calllog.mark_voice(vkey, True)

    calllog.record_turn(manifest, 0, {
        "at": calllog.iso(), "kind": "opening", "transcript": None, "reply": reply,
        "signals": {}, "phase": state["phase"], "state": state, "model": info.get("model"),
        "timings": {"llm_ms": int((time.time() - started) * 1000)},
    })
    return _ok()


def _on_prompt(connection_id, message, manifest):
    """One finished utterance from the patient."""
    started = time.time()
    raw = (message.get("voicePrompt") or "").strip()
    if not raw:
        return _ok()

    config = manifest.get("config") or {}
    # Scrubbed BEFORE it is stored, logged, or sent to the model — there is no path where the
    # raw utterance is persisted first and cleaned afterwards.
    transcript = _scrub_inbound(manifest, raw, config.get("language", "en"))
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
        _forget_vault(manifest)
        # Finalise here rather than letting $disconnect do it. Twilio closes the socket a moment
        # later and would otherwise stamp "disconnected" over the real reason, making voicemails
        # uncountable from the index without opening every manifest.
        calllog.finish(manifest, "voicemail")
        return _ok()

    elapsed = _elapsed_s(manifest)
    try:
        reply, state, info = brain.turn(state, transcript, _model_config(manifest, config),
                                        elapsed_s=elapsed)
    except gateway.AIError as e:
        # One bad turn is not a dead call. Ending the conversation because a single generation
        # stalled costs the patient the rest of their session over something usually momentary.
        # So the first failure gets a human sentence and the call carries on; a second failure in
        # a row means something is actually wrong, and stopping beats apologising on a loop.
        fails = int(manifest.get("ai_failures") or 0) + 1
        manifest["ai_failures"] = fails
        reason = "rate-limited" if isinstance(e, gateway.AllRateLimited) else "ai-unavailable"
        if fails >= 2:
            return _abandon(connection_id, manifest, config, e, reason)
        print("WARN: turn %d failed (%s); asking them to repeat: %s" % (seq, reason, e))
        _send(connection_id, relay.say(lost_thread(config)))
        calllog.record_turn(manifest, seq, {
            "at": calllog.iso(), "kind": "turn-failed", "transcript": transcript, "reply": None,
            "signals": {}, "phase": state.get("phase"), "state": state, "model": None,
            "timings": {"llm_ms": int((time.time() - started) * 1000)},
            "error": reason, "elapsed_s": elapsed,
        })
        return _ok()
    except Exception as e:  # noqa: BLE001 — a dropped turn must not drop the call
        print("ERROR: turn failed: %s" % e)
        _send(connection_id, relay.say(lost_thread(config)))
        return _ok()

    manifest["ai_failures"] = 0          # a good turn clears the slate
    if reply:
        _send(connection_id, relay.say(_restore_outbound(manifest, reply)))
    elif not info.get("ended"):
        # An empty reply that is NOT the end of the call would be heard as silence, which is the
        # one thing a voice call must never do. (At the end it is correct: the goodbye already
        # went out and the hang-up follows.)
        print("WARN: empty reply on %s; asking them to repeat" % manifest["call_id"])
        _send(connection_id, relay.say(lost_thread(config)))

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
        _forget_vault(manifest)
        calllog.finish(manifest, "engine-ended")
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
        if manifest:
            _forget_vault(manifest)
            if manifest.get("status") != "completed":
                calllog.finish(manifest, "disconnected")
            else:
                calllog._put_json(calllog._manifest_key(manifest["call_id"]), manifest)
        return _ok()

    message = relay.parse(event.get("body") or "{}")
    kind = message.get("type")
    manifest = None          # bound before the try so the catch below can still localise its line

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
            description = str(message.get("description") or "")
            print("TWILIO ERROR: %s" % description)
            # A TTS failure is the one error worth remembering. The voice cannot be changed on a
            # live call, so all we can do is make sure the NEXT call does not repeat it.
            vkey = (manifest.get("telephony") or {}).get("voice_key")
            if vkey and any(w in description.lower()
                            for w in ("tts", "voice", "speech synthesis", "64111", "64112")):
                calllog.mark_voice(vkey, False, description)
            return _ok()
        return _ok()

    except Exception as e:  # noqa: BLE001 — never let an exception drop a live call silently
        print("ERROR: unhandled %s on %s: %s" % (kind, connection_id, e))
        _send(connection_id, relay.say(lost_thread((manifest or {}).get("config"))))
        return _ok()
