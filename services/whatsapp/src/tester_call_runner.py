"""
MEET_RUDI — meetrudi-tester-call-runner (Rudi-initiated calls for the tester cohort).

Runs on an EventBridge tick. Two jobs, in this order:

1. **Reconcile calls that have ended.** The console's ledger only advanced when the tester's
   browser polled /call/status, so a call Rudi placed himself was never counted. Here the call
   manifest is the source of truth: a CONNECTED call deducts one, whoever started it. The same
   pass turns an ended call into the follow-up it earned.

2. **Place the calls Rudi owes.** Two reasons exist today:

     promise            he told someone on a SET_NEARTERM_GOAL / GOAL_FOLLOWUP call that he
                        would ring back at a time, and that promise is now due
     whatsapp_reminder  a GET_TO_KNOW call found the person's focus area and asked them to
                        start the WhatsApp thread — and 22 hours later they still haven't, so
                        the coaching never began

Before dialling a conversation it asks the model one cheap question. With no headroom the call
still happens, but as a single spoken line: ringing someone and then abandoning them on turn one
is worse than a short, honest message.

Every gate the console honours is honoured here too — consent, quiet hours, the per-tester call
ceiling, the cohort's daily cap, and the one-call-at-a-time queue. The dispatcher re-checks the
non-negotiable ones itself, so this cannot talk its way past them.
"""

import os
import json
import datetime
import urllib.error
import urllib.request

import boto3

import store
import gateway
import tester_store
from tester_store import TesterStore

_s3 = boto3.client("s3")
_secrets = boto3.client("secretsmanager")

DATA_BUCKET = os.environ["DATA_BUCKET"]
SALT = os.environ.get("PSEUDONYMIZE_SALT", "meetrudi-pilot-salt-change-me")
CALL_PREFIX = os.environ.get("CALL_PREFIX", "calls")
CALL_DISPATCH_URL = os.environ.get("CALL_DISPATCH_URL", "")
CALL_DISPATCH_SECRET = os.environ.get("CALL_DISPATCH_SECRET", "meetrudi/call/dispatch-token")
CALL_MAX_SECONDS = int(os.environ.get("TESTER_CALL_MAX_SECONDS", "300"))
DEFAULT_TZ = os.environ.get("DEFAULT_TZ", store.DEFAULT_TZ)

# How long a get-to-know call waits for the person to open WhatsApp before Rudi rings to nudge
# them. 22h, not 24h: the reminder has to land while the conversation is still young.
WHATSAPP_GRACE_HOURS = int(os.environ.get("TESTER_WHATSAPP_GRACE_HOURS", "22"))

CALL_LANGUAGE = {"nl-BE": os.environ.get("TESTER_CALL_LANG_NL", "nl-BE"),
                 "en": os.environ.get("TESTER_CALL_LANG_EN", "en-GB")}

# Spoken verbatim when there is no AI headroom. Short on purpose: it exists to carry one fact.
FALLBACK_LINE = {
    "en": os.environ.get("TESTER_FALLBACK_LINE_EN",
                         "Hi, this is Rudi. I'm calling to remind you that we agreed to jump on "
                         "the WhatsApp chat, so I am waiting for you there. Talk soon!"),
    "nl-BE": os.environ.get("TESTER_FALLBACK_LINE_NL",
                            "Hallo, met Rudi. Ik bel je even om je eraan te herinneren dat we "
                            "hadden afgesproken verder te chatten op WhatsApp. Ik wacht daar op "
                            "je. Tot snel!"),
}

STORE = TesterStore(_s3, DATA_BUCKET)
WA = store.ConversationStore(_s3, DATA_BUCKET)
_cache: dict = {}


# --------------------------------------------------------------------------- plumbing
def _secret(secret_id):
    if secret_id in _cache:
        return _cache[secret_id]
    try:
        raw = _secrets.get_secret_value(SecretId=secret_id).get("SecretString", "") or ""
    except Exception as e:  # noqa: BLE001
        print("WARN secret %s unavailable (%s)" % (secret_id, type(e).__name__))
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        value = {"token": raw}
    _cache[secret_id] = value
    return value


def _manifest(call_id):
    try:
        raw = _s3.get_object(Bucket=DATA_BUCKET,
                             Key="%s/%s/manifest.json" % (CALL_PREFIX, call_id))["Body"].read()
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 - not written yet, or gone
        return None


def _index_rows(day):
    """Light rows for one day. Reading the index rather than every manifest keeps this cheap as
    the cohort accumulates calls."""
    rows = []
    token = None
    prefix = "%s/_index/%s/" % (CALL_PREFIX, day)
    while True:
        kw = {"Bucket": DATA_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = _s3.list_objects_v2(**kw)
        for item in resp.get("Contents", []) or []:
            try:
                raw = _s3.get_object(Bucket=DATA_BUCKET, Key=item["Key"])["Body"].read()
                row = json.loads(raw)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:  # noqa: BLE001 - one unreadable row must not stop the pass
                continue
        if not resp.get("IsTruncated"):
            return rows
        token = resp.get("NextContinuationToken")
        if not token:
            return rows


def _dispatch(config):
    """POST to the call dispatcher. Returns (ok, payload_or_reason)."""
    if not CALL_DISPATCH_URL:
        return False, "dispatch_not_configured"
    creds = _secret(CALL_DISPATCH_SECRET) or {}
    token = creds.get("token") or creds.get("password") or ""
    if not token:
        return False, "dispatch_not_configured"
    body = json.dumps({"token": token, "config": config}).encode("utf-8")
    req = urllib.request.Request(CALL_DISPATCH_URL, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        print("ERROR dispatch HTTP %s" % e.code)
        return False, "dispatch_failed"
    except Exception as e:  # noqa: BLE001
        print("ERROR dispatch %s" % type(e).__name__)
        return False, "dispatch_failed"
    if not payload.get("ok"):
        return False, payload.get("skipped") or payload.get("error") or "dispatch_failed"
    return True, payload


# --------------------------------------------------------------------------- 1. reconcile
MACHINE_ANSWERS = {"machine_start", "machine_end_beep", "machine_end_silence",
                   "machine_end_other", "fax"}


# A call that ended because OUR side broke is not a call the person had. Charging it to their
# ledger bills them for our outage: the 12:48 incident died on turn one with ai-unavailable,
# which "turns > 0" alone would have scored as a completed conversation.
OUR_FAULT_ENDINGS = {"ai-unavailable", "rate-limited", "unknown-call", "voicemail"}


def _outcome_of(manifest):
    """What actually happened, in the console's vocabulary."""
    manifest = manifest or {}
    tel = manifest.get("telephony") or {}
    if str(tel.get("answered_by") or "") in MACHINE_ANSWERS:
        return "voicemail"
    status = str(tel.get("call_status") or "")
    if status in ("no-answer", "busy"):
        return "no_answer"
    if status in ("failed", "canceled") or tel.get("error"):
        return "failed"
    if str(manifest.get("end_reason") or "") in OUR_FAULT_ENDINGS:
        return "voicemail" if manifest.get("end_reason") == "voicemail" else "failed"
    turns = int((manifest.get("totals") or {}).get("turns") or 0)
    return "connected" if turns > 0 else "no_answer"


def _follow_up_from(manifest, tester, ended_at):
    """Turn a finished call into the call it earned, if any."""
    outcome = manifest.get("outcome") or {}
    config = manifest.get("config") or {}
    goal = str(config.get("call_goal") or "").upper()

    # A promise Rudi made aloud. Same rule as WhatsApp: inside 24h, never in quiet hours.
    minutes = outcome.get("check_in_minutes")
    if minutes:
        try:
            when = ended_at + datetime.timedelta(minutes=int(minutes))
        except (TypeError, ValueError):
            when = None
        if when:
            when = store.next_social_start(when, store._tz(DEFAULT_TZ))
            STORE.schedule_call(tester.tester_id, store.to_iso(when), "promise",
                                note=str(outcome.get("check_in_about") or "")[:200])
            print("RUNNER promise scheduled tid=%s at=%s" % (tester.tester_id, store.to_iso(when)))
            return "promise"

    # A get-to-know call that actually found the focus area hands over to WhatsApp. If the
    # handover doesn't happen, the person never gets coached at all — so it is worth a call.
    if goal == "GET_TO_KNOW" and outcome.get("goal_domain"):
        when = store.next_social_start(
            ended_at + datetime.timedelta(hours=WHATSAPP_GRACE_HOURS), store._tz(DEFAULT_TZ))
        STORE.schedule_call(tester.tester_id, store.to_iso(when), "whatsapp_reminder",
                            note=str(outcome.get("goal_domain") or "")[:200],
                            since=store.to_iso(ended_at))
        print("RUNNER whatsapp reminder scheduled tid=%s at=%s" % (tester.tester_id,
                                                                   store.to_iso(when)))
        return "whatsapp_reminder"
    return ""


def reconcile(now):
    """Fold finished calls into the ledger and schedule what they earned."""
    done = STORE.reconciled_calls()
    days = {now.date().isoformat(), (now - datetime.timedelta(days=1)).date().isoformat()}
    counted = scheduled = 0

    for day in sorted(days):
        for row in _index_rows(day):
            call_id = row.get("call_id") or ""
            tid = row.get("tester_id") or ""
            if not call_id or not tid or call_id in done:
                continue
            if row.get("status") != "completed":
                continue        # still running; it will be picked up on a later tick
            manifest = _manifest(call_id)
            if not manifest:
                continue
            tester = STORE.get(tid)
            if tester is None:
                STORE.mark_reconciled(call_id)
                continue

            outcome = _outcome_of(manifest)
            if outcome == "connected":
                # Every connected call counts, whoever placed it (product decision).
                tester.calls_used = min(tester.calls_max, tester.calls_used + 1)
                counted += 1
            tester.last_call_outcome = outcome
            tester.last_call_at = manifest.get("ended_at") or store.iso_now()
            if outcome == "connected" and tester.track_call != "done":
                tester.track_call = "done"
            STORE.put(tester)

            if outcome == "connected":
                try:
                    ended_at = store.parse_iso(manifest.get("ended_at") or store.iso_now())
                except (ValueError, TypeError):
                    ended_at = now
                if _follow_up_from(manifest, tester, ended_at):
                    scheduled += 1

            STORE.mark_reconciled(call_id)
            print("RUNNER reconciled call=%s tid=%s outcome=%s used=%d"
                  % (call_id, tid, outcome, tester.calls_used))
    return counted, scheduled


# --------------------------------------------------------------------------- 2. place calls
def _messaged_since(tester, since_iso):
    """Did they start the WhatsApp thread after the call? Cancels the reminder if so."""
    if not tester.wa_user_id or not since_iso:
        return False
    meta = WA.get_meta(tester.wa_user_id)
    if meta is None or not meta.last_inbound_at:
        return False
    try:
        return store.parse_iso(meta.last_inbound_at) > store.parse_iso(since_iso)
    except (ValueError, TypeError):
        return False


def _config_for(entry, tester, speak_only=""):
    goal = "GOAL_FOLLOWUP" if entry.get("reason") == "promise" else "SET_NEARTERM_GOAL"
    notes = ""
    if entry.get("reason") == "whatsapp_reminder":
        notes = ("They had a first call about %s and agreed to continue on WhatsApp, but have "
                 "not sent a message yet. Help them set one small near-term goal, and before you "
                 "say goodbye ask them again to send you any WhatsApp message so the two of you "
                 "can carry on there." % (entry.get("note") or "their goal"))
    elif entry.get("note"):
        notes = "You promised to call back about: %s. Ask about that first." % entry["note"]

    config = {
        "to": tester.phone,                     # hard-locked to the registered number
        "user_name": tester.first_name,
        "language": CALL_LANGUAGE.get(tester.locale, "en-GB"),
        "timezone": DEFAULT_TZ,
        "consent_state": "granted" if tester.consent_health else "unknown",
        "status": "active",
        "topic": tester.goal,
        "notes": notes,
        "call_goal": goal,
        "max_seconds": CALL_MAX_SECONDS,
        "machine_detection": True,
        "tester_id": tester.tester_id,
        "placed_by": "runner:%s" % entry.get("reason"),
    }
    if speak_only:
        config["speak_only"] = speak_only
    return config


def _skip(entry, why):
    print("RUNNER skip tid=%s reason=%s (%s)" % (entry.get("tester_id"), entry.get("reason"), why))


def place_due(now):
    """Dial what is due. Returns (placed, skipped)."""
    settings = STORE.settings()
    due = STORE.due_calls(now)
    if not due:
        return 0, 0
    if settings.get("calling_paused"):
        print("RUNNER calling paused; %d call(s) held" % len(due))
        return 0, len(due)

    placed = skipped = 0
    cap = int(settings.get("daily_call_cap") or tester_store.DAILY_CALL_CAP)

    for entry in due:
        tid = entry.get("tester_id") or ""
        tester = STORE.get(tid)
        if tester is None or tester.status == "revoked":
            STORE.cancel_calls(tid, entry.get("reason", ""))
            skipped += 1
            continue
        if tester.calls_left() <= 0:
            _skip(entry, "no calls left on their ledger")
            STORE.cancel_calls(tid, entry.get("reason", ""))
            skipped += 1
            continue
        if STORE.calls_today() >= cap:
            _skip(entry, "cohort daily cap reached; will retry next tick")
            skipped += 1
            continue
        # One call at a time, exactly as the console promises its testers.
        if (STORE.queue().get("active") or {}).get("tester_id"):
            _skip(entry, "another call is on the line")
            skipped += 1
            continue
        # The reason may simply have evaporated.
        if entry.get("reason") == "whatsapp_reminder" and _messaged_since(tester, entry.get("since")):
            print("RUNNER whatsapp reminder cancelled tid=%s (they messaged)" % tid)
            STORE.cancel_calls(tid, "whatsapp_reminder")
            skipped += 1
            continue

        # Enough model capacity for a conversation, or just the one line?
        line = "" if gateway.has_headroom() else (
            FALLBACK_LINE.get(tester.locale) or FALLBACK_LINE["en"])
        ok, result = _dispatch(_config_for(entry, tester, speak_only=line))
        if not ok:
            if result == "quiet-hours":
                when = store.next_social_start(now, store._tz(DEFAULT_TZ))
                STORE.schedule_call(tid, store.to_iso(when), entry.get("reason", ""),
                                    note=entry.get("note", ""), since=entry.get("since", ""))
                _skip(entry, "quiet hours; moved to %s" % store.to_iso(when))
            else:
                _skip(entry, "dispatch refused: %s" % result)
            skipped += 1
            continue

        STORE.cancel_calls(tid, entry.get("reason", ""))
        STORE.bump_calls_today()
        placed += 1
        print("RUNNER placed tid=%s reason=%s call=%s mode=%s"
              % (tid, entry.get("reason"), result.get("call_id"),
                 "speak_only" if line else "conversation"))
    return placed, skipped


# --------------------------------------------------------------------------- entry point
def handler(event, context):
    now = store.now_dt()
    counted = scheduled = placed = skipped = 0
    try:
        counted, scheduled = reconcile(now)
    except Exception as e:  # noqa: BLE001 - reconciliation must not block dialling
        print("ERROR reconcile: %s" % e)
    try:
        placed, skipped = place_due(now)
    except Exception as e:  # noqa: BLE001
        print("ERROR place_due: %s" % e)
    print("RUNNER tick counted=%d scheduled=%d placed=%d skipped=%d"
          % (counted, scheduled, placed, skipped))
    return {"ok": True, "counted": counted, "scheduled": scheduled,
            "placed": placed, "skipped": skipped}
