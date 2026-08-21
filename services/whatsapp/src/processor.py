"""
MEET_RUDI — meetrudi-wa-processor handler.

Consumes inbound WhatsApp messages from the FIFO queue, pseudonymizes the phone number, and
persists the message into the ConversationStore. Then, when AI_RESPONDER is on (default), runs
the "Real Rudi" AI responder (learn → goal → commit, ported from meetrudi-rudi-chat) and sends
the reply. With AI_RESPONDER off it is persist-only (operator-console mode) — a human replies
from the console instead.

Either way every message is stored, so the operator console always sees the live conversation.
PII (§5): raw phone stays in meta.json (AWS-EU plane); logs carry the pseudonymous userId only.
"""

import os
import json

import boto3

import store
import i18n
import provider
import gateway
import deid
import responder
import personality

_s3 = boto3.client("s3")
DATA_BUCKET = os.environ["DATA_BUCKET"]
SALT = os.environ.get("PSEUDONYMIZE_SALT", "meetrudi-pilot-salt")
AI_RESPONDER = os.environ.get("AI_RESPONDER", "true").lower() == "true"

STORE = store.ConversationStore(_s3, DATA_BUCKET)


# De-identification on the WhatsApp path. Same two tiers as the call service, same reasoning,
# but the session boundary is different: a call ends when the line drops, whereas a WhatsApp
# conversation has no natural end. The 24h free-form window is the session — the same boundary
# the product already uses for everything else — so a new window means a fresh vault and no
# alias mapping outlives the window that created it.
_DETECTOR = deid.HeuristicDetector()


def _session_vault(prior_meta):
    """(vault, is_new_session). A closed window means the previous session is over."""
    if prior_meta is None or not prior_meta.is_in_window():
        return deid.AliasVault(), True
    return deid.AliasVault.from_dict(prior_meta.alias_vault or None), False


def _scrub_inbound(vault, text, locale):
    """Clean before ANY persistence: this runs ahead of record_inbound, so the raw message is
    never written to S3, never shown in the operator console and never reaches a model."""
    clean, found = deid.redact(text or "")
    if found:
        print("PII redacted %s" % sorted(found))
    return vault.mask(clean, _DETECTOR, locale), found


def _restore_outbound(vault, text):
    """Placeholders back to real names in the last step before the send channel."""
    out = vault.unmask(text or "", fallback="them")
    if deid.has_placeholder(out):
        # Belt and braces: never let a raw placeholder reach a patient's phone.
        print("WARN: unresolved placeholder suppressed before send")
        out = deid.PLACEHOLDER_RE.sub("them", out)
    return out


def _to_message(msg: dict) -> store.Message:
    return store.Message(
        id=msg.get("provider_msg_id") or store.new_message_id(),
        direction="in",
        type=msg.get("type", "text"),
        text=msg.get("text", "") or "",
        media=msg.get("media", []) or [],
        twilio_sid=msg.get("provider_msg_id"),
        delivery_status="received",
    )


def _reply_and_persist(uid: str, phone: str, text: str, meta, vault) -> None:
    """Run the AI responder for one turn, send the reply, persist outbound + AI state + locale.

    `text` is already de-identified. The reply is stored in that same masked form and only
    un-masked for the send, so what sits in S3 stays non-identifying while the patient still
    reads a natural message.
    """
    locale = meta.locale or i18n.DEFAULT_LOCALE
    pblock = personality.resolve_block(meta.persona)   # operator-chosen persona (or default)
    reply, new_state, info = responder.respond(meta.ai_state, text, locale=locale,
                                               personality_block=pblock)
    new_locale = info.get("lang") or locale   # "last used language" (falls back to current)
    provider.send_text(phone, _restore_outbound(vault, reply))
    out = store.Message(id=store.new_message_id(), direction="out", type="text",
                        text=reply, operator_id="ai:rudi")
    STORE.record_outbound(uid, out, ai_state=new_state, locale=new_locale,
                          alias_vault=vault.to_dict())
    print("AI uid=%s phase=%s lang=%s model=%s"
          % (uid, info.get("phase"), new_locale, info.get("model")))

    # End of a session → refresh the user profile (goal + a one-line 'recent development').
    if info.get("phase") == "concluded":
        _update_profile(uid, new_state)


def _update_profile(uid: str, ai_state: dict) -> None:
    try:
        development = responder.summarize(ai_state.get("history", []))
    except Exception as e:  # noqa: BLE001 - summary is best-effort
        print("WARN summarize uid=%s: %s" % (uid, e))
        development = None
    prof = STORE.write_profile(uid, extracted_goal=ai_state.get("goal"), development=development)
    print("PROFILE uid=%s goal=%s proactivity=%s" % (uid, bool(prof.get("extracted_goal_commitment")),
                                                     prof.get("proactivity_index")))


def handler(event, context):
    for record in event.get("Records", []):
        phone = ""
        try:
            msg = json.loads(record["body"])
            phone = msg.get("user_phone", "")
            if not phone:
                continue
            uid = store.user_id(phone, SALT)
            # Read the contact BEFORE recording, so the message can be scrubbed on the way in
            # rather than cleaned up afterwards.
            prior = STORE.get_meta(uid)
            vault, new_session = _session_vault(prior)
            locale = (prior.locale if prior else "") or i18n.DEFAULT_LOCALE
            if new_session and prior is not None:
                print("SESSION new window uid=%s — alias vault reset" % uid)

            scrubbed, _found = _scrub_inbound(vault, msg.get("text", "") or "", locale)
            meta = STORE.record_inbound(uid, phone, _to_message(dict(msg, text=scrubbed)))
            meta.alias_vault = vault.to_dict()
            locale = meta.locale or i18n.DEFAULT_LOCALE
            print("INBOUND uid=%s type=%s sid=%s" % (uid, msg.get("type"), msg.get("provider_msg_id")))

            if not AI_RESPONDER:
                continue  # operator-console mode: a human answers from the console

            text = scrubbed
            if not text:  # media/non-text: acknowledge (localized), don't advance the AI session
                provider.send_text(phone, i18n.t("media_ack", locale, kind=(msg.get("type") or "message")))
                continue
            _reply_and_persist(uid, phone, text, meta, vault)

        except gateway.AllRateLimited:
            if phone:
                try:
                    provider.send_text(phone, i18n.t("tired", _locale_for(phone)))
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001 - one bad record must not poison the batch
            print("ERROR processor: %s" % e)
            if phone:
                try:
                    provider.send_text(phone, i18n.t("error", _locale_for(phone)))
                except Exception:  # noqa: BLE001
                    pass
    return {"ok": True}


def _locale_for(phone: str) -> str:
    """Best-effort locale for error/rate-limit messages (never raises)."""
    try:
        meta = STORE.get_meta(store.user_id(phone, SALT))
        return (meta.locale if meta else "") or i18n.DEFAULT_LOCALE
    except Exception:  # noqa: BLE001
        return i18n.DEFAULT_LOCALE
