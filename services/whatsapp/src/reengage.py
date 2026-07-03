"""
MEET_RUDI — meetrudi-wa-reengage handler (the keep-session-warm runner).

Runs on an EventBridge schedule (every few minutes). It does NOT recompute who to reach — that
was precomputed on each message event (store.compute_next_proactive → meta.next_proactive_at /
next_proactive_kind). The runner just polls who is due and acts:

  - kind "nudge"    → free-form in-window check-in (keeps the 24h window alive; herds replies
                      into social hours). Only sent while the window is open.
  - kind "template" → out-of-window re-engagement; needs an approved WhatsApp template
                      (TEMPLATE_CONTENT_SID). Skipped with a log line until one is configured.

record_outbound(proactive_kind=…) marks the send and reschedules the conversation. Opted-out
numbers (keep_warm=false) are never scheduled, and are re-checked here as a belt-and-braces guard.
"""

import os

import boto3

import store
import i18n
import provider

_s3 = boto3.client("s3")
DATA_BUCKET = os.environ["DATA_BUCKET"]
SALT = os.environ.get("PSEUDONYMIZE_SALT", "meetrudi-pilot-salt")
TEMPLATE_CONTENT_SID = os.environ.get("TEMPLATE_CONTENT_SID", "")

STORE = store.ConversationStore(_s3, DATA_BUCKET)


def _eligible(meta) -> bool:
    return bool(meta.keep_warm and meta.status == "active" and meta.consent_state == "granted"
                and meta.phone)


def _send_nudge(meta, now) -> bool:
    if not meta.is_in_window(now):
        return False  # window closed — can't free-form; compute will switch this to a template
    text = i18n.t("nudge", meta.locale or i18n.DEFAULT_LOCALE)
    provider.send_text(meta.phone, text)
    out = store.Message(id=store.new_message_id(), direction="out", type="text",
                        text=text, operator_id="system:nudge")
    STORE.record_outbound(meta.user_id, out, proactive_kind="nudge")
    return True


def _send_template(meta) -> bool:
    if not TEMPLATE_CONTENT_SID:
        print("SKIP template uid=%s (no approved TEMPLATE_CONTENT_SID configured yet)" % meta.user_id)
        return False
    provider.send_template(meta.phone, TEMPLATE_CONTENT_SID)
    out = store.Message(id=store.new_message_id(), direction="out", type="template",
                        text="[template re-engagement]", operator_id="system:template")
    STORE.record_outbound(meta.user_id, out, proactive_kind="template")
    return True


def handler(event, context):
    now = store.now_dt()
    due = STORE.list_due(now)
    sent = skipped = 0
    for meta in due:
        try:
            if not _eligible(meta):
                skipped += 1
                continue
            if meta.next_proactive_kind == "nudge":
                ok = _send_nudge(meta, now)
            elif meta.next_proactive_kind == "template":
                ok = _send_template(meta)
            else:
                ok = False
            if ok:
                sent += 1
                print("REENGAGE uid=%s kind=%s lang=%s" % (meta.user_id, meta.next_proactive_kind, meta.locale))
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001 - one bad contact must not stop the batch
            skipped += 1
            print("ERROR reengage uid=%s: %s" % (meta.user_id, e))
    print("REENGAGE tick due=%d sent=%d skipped=%d" % (len(due), sent, skipped))
    return {"ok": True, "due": len(due), "sent": sent, "skipped": skipped}
