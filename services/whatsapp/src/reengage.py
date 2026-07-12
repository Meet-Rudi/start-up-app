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
import json

import boto3

import store
import i18n
import provider
import responder
import personality

_s3 = boto3.client("s3")
DATA_BUCKET = os.environ["DATA_BUCKET"]
SALT = os.environ.get("PSEUDONYMIZE_SALT", "meetrudi-pilot-salt")
# Re-engagement templates: locale -> [approved Twilio Content SID, ...] in S3 (editable without
# a redeploy of code). Multiple SIDs per locale = rotation. The {{1}} variable carries the goal.
TEMPLATES_KEY = os.environ.get("TEMPLATES_CONFIG_KEY", "config/wa_templates.json")
INSERT_GOAL = os.environ.get("REENGAGE_INSERT_GOAL", "true").lower() == "true"
_tpl_cache: dict = {}

STORE = store.ConversationStore(_s3, DATA_BUCKET)


def _load_templates() -> dict:
    """Locale -> [ContentSid,...] map for re-engagement templates (from S3, cached)."""
    if "map" in _tpl_cache:
        return _tpl_cache["map"]
    try:
        obj = _s3.get_object(Bucket=DATA_BUCKET, Key=TEMPLATES_KEY)
        data = json.loads(obj["Body"].read().decode("utf-8"))
        tmap = data.get("reengage", {}) if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001 - missing/broken config => no templates yet
        print("INFO templates config unavailable (%s)" % e)
        tmap = {}
    _tpl_cache["map"] = tmap
    return tmap


def _pick_template_sid(locale: str, rotation_index: int):
    """Choose an approved SID for the locale (fallback to default), rotating across variants."""
    tmap = _load_templates()
    sids = tmap.get(locale) or tmap.get(i18n.DEFAULT_LOCALE) or []
    if not sids:
        return None
    return sids[rotation_index % len(sids)]


def _goal_variable(meta) -> str:
    """Value for the template's {{1}}: the stored commitment if we have one, else a
    per-language generic phrase (keeps the sentence grammatical in every language)."""
    if INSERT_GOAL:
        goal = (STORE.get_profile(meta.user_id) or {}).get("extracted_goal_commitment")
        if goal:
            return goal
    return i18n.t("generic_goal", meta.locale or i18n.DEFAULT_LOCALE)


def _eligible(meta) -> bool:
    return bool(meta.keep_warm and meta.status == "active" and meta.consent_state == "granted"
                and meta.phone)


def _history_from_messages(msgs, limit=20):
    """Build a [{role,content}] history from stored messages for summarization."""
    out = []
    for m in msgs[-limit:]:
        content = m.text or ("[photo]" if m.type in ("image", "video")
                             else "[voice note]" if m.type == "audio" else "")
        if content:
            out.append({"role": "user" if m.direction == "in" else "assistant", "content": content})
    return out


def _refresh_profile_if_stale(meta) -> dict:
    """Before re-engaging, don't trust the stored profile: if any message is newer than
    last_profile_update_at, re-summarize from the messages and rewrite the profile. If the
    profile is already current (last_profile_update_at >= newest message), use it as-is."""
    if not STORE.profile_is_stale(meta.user_id):
        return STORE.get_profile(meta.user_id)
    existing = STORE.get_profile(meta.user_id)
    try:
        development = responder.summarize(_history_from_messages(STORE.list_messages(meta.user_id)))
    except Exception as e:  # noqa: BLE001 - keep prior summary on AI error
        print("WARN refresh summarize uid=%s: %s" % (meta.user_id, e))
        development = existing.get("most_recent_development")
    goal = (meta.ai_state or {}).get("goal") or existing.get("extracted_goal_commitment")
    prof = STORE.write_profile(meta.user_id, extracted_goal=goal, development=development)
    print("PROFILE refresh uid=%s (reconciled newer messages)" % meta.user_id)
    return prof


def _send_nudge(meta, now) -> bool:
    if not meta.is_in_window(now):
        return False  # window closed — can't free-form; compute will switch this to a template
    locale = meta.locale or i18n.DEFAULT_LOCALE
    # Reconcile the profile with any messages newer than its last update, THEN reach out with it.
    profile = _refresh_profile_if_stale(meta)
    # Prefer a contextual AI reach-out (references their goal + last development); canned fallback.
    ai_state = None
    try:
        text, ai_state, _ = responder.reach_out(
            meta.ai_state, locale,
            goal=profile.get("extracted_goal_commitment"),
            development=profile.get("most_recent_development"),
            personality_block=personality.resolve_block(meta.persona))
    except Exception as e:  # noqa: BLE001 - rate-limited / AI error → safe canned nudge
        print("WARN reachout uid=%s fell back to canned nudge: %s" % (meta.user_id, e))
        text = i18n.t("nudge", locale)
    provider.send_text(meta.phone, text)
    out = store.Message(id=store.new_message_id(), direction="out", type="text",
                        text=text, operator_id="ai:nudge")
    STORE.record_outbound(meta.user_id, out, proactive_kind="nudge", ai_state=ai_state, locale=locale)
    return True


def _send_template(meta) -> bool:
    locale = meta.locale or i18n.DEFAULT_LOCALE
    sid = _pick_template_sid(locale, meta.reengage_count)
    if not sid:
        print("SKIP template uid=%s (no approved template SID for locale=%s)" % (meta.user_id, locale))
        return False
    goal = _goal_variable(meta)
    provider.send_template(meta.phone, sid, {"1": goal})
    out = store.Message(id=store.new_message_id(), direction="out", type="template",
                        text="[template re-engagement] " + goal, operator_id="system:template")
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
