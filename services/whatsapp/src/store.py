"""
MEET_RUDI — ConversationStore (S3-backed system of record for the operator console).

The storage seam for 1:1 WhatsApp conversations. The processor (inbound) and the console API
(read + operator send) only touch this module, so the layout stays swappable.

Layout (all under a single EU-region data bucket):

    conversations/{userId}/meta.json                       # contact record + conversation state
    conversations/{userId}/messages/{ts_ms}-{msgId}.json   # ONE object per message

Design choices:
- One object per message → no S3 read-modify-write append race between the inbound processor
  and the outbound send path. A thread is the `messages/` prefix, sorted lexicographically
  (the zero-padded millisecond timestamp makes key order == time order).
- Conversations are keyed by the pseudonymous `userId` (HMAC of the phone). Raw phone (PII)
  lives only inside meta.json, which stays in the AWS-EU plane — never in S3 keys or logs (§5).
- Pure stdlib + an injected S3 client (boto3 at runtime, an in-memory fake in tests). No
  vendor lock beyond the S3 object API.
"""

from __future__ import annotations

import os
import json
import hmac
import uuid
import hashlib
import datetime
import zoneinfo
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional, Protocol

PREFIX = "conversations"
WINDOW_HOURS = 24  # WhatsApp free-form reply window after the user's last inbound message.

# --- Proactive (nudge / re-engagement) policy -------------------------------------------------
# We never keep a session "live" past the 24h window by force — only a user reply reopens it.
# The compliant bridge is: a free-form NUDGE placed in the last SOCIAL-hours slot before the
# window closes (herds replies into daytime, no template cost), and only if that lapses, a paid
# TEMPLATE re-engagement. Timing is precomputed on every message event and stored on meta; the
# 5-min runner just checks who's due. All proactive sends are quiet-hours suppressed; reactive
# replies (operator now, AI later) are NOT — they are unrestricted.
DEFAULT_TZ = "Europe/Brussels"       # pilot cohort default; per-contact override on meta.timezone
QUIET_START = "21:30"                # no proactive sends inside [QUIET_START, QUIET_END) local
QUIET_END = "06:30"
NUDGE_LEAD = datetime.timedelta(hours=2)       # aim to nudge ~this long before the window closes
PRE_QUIET_BUFFER = datetime.timedelta(minutes=5)   # keep the nudge clearly before quiet starts
MIN_TEMPLATE_GAP = datetime.timedelta(hours=48)    # cadence cap (~2–3×/week) between templates
MAX_TEMPLATE_MISSES = 3              # consecutive unanswered templates → dormant (protect quality)

# Test mode: schedule a nudge at last_inbound + TEST_LEAD (bypassing anti-drift/quiet hours) so
# the keep-warm loop can be observed in minutes. Leave off in production.
TEST_MODE = os.environ.get("PROACTIVE_TEST_MODE", "false").lower() == "true"
TEST_LEAD = datetime.timedelta(minutes=int(os.environ.get("PROACTIVE_TEST_LEAD_MIN", "3")))


# --------------------------------------------------------------------------- time helpers
def now_dt() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def to_iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat()


def iso_now() -> str:
    return to_iso(now_dt())


def ms_from_iso(iso: str) -> int:
    """Sortable millisecond timestamp from an ISO-8601 string."""
    dt = datetime.datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


# --------------------------------------------------------------------------- pseudonymization
def user_id(phone: str, salt: str) -> str:
    """Stable, one-way pseudonym for a phone number (same scheme the processor has always used)."""
    return "wa_" + hmac.new(salt.encode(), phone.encode(), hashlib.sha256).hexdigest()[:24]


# --------------------------------------------------------------------------- data model
@dataclass
class Message:
    id: str
    direction: str                       # "in" | "out"
    type: str = "text"                   # "text" | "image" | "audio" | "media"
    text: str = ""
    at: str = field(default_factory=iso_now)
    media: list[dict[str, Any]] = field(default_factory=list)
    twilio_sid: Optional[str] = None
    delivery_status: Optional[str] = None   # queued | sent | delivered | read | failed
    operator_id: Optional[str] = None       # set on outbound (who sent it)

    @property
    def ts_ms(self) -> int:
        return ms_from_iso(self.at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        known = {f: d.get(f) for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)


@dataclass
class ContactMeta:
    user_id: str
    phone: str = ""                      # PII — AWS-EU plane only, never in keys/logs
    display_name: str = ""
    locale: str = "en"
    consent_state: str = "unknown"       # unknown | granted | revoked
    persona: str = ""                    # which "Rudi"
    assigned_number: str = ""            # our WhatsApp sender this contact is pinned to
    status: str = "active"               # active | archived | blocked
    keep_warm: bool = True               # operator can stop proactive keep-warm per number
    tags: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=iso_now)
    last_inbound_at: str = ""
    last_outbound_at: str = ""
    window_open_until: str = ""          # last_inbound_at + WINDOW_HOURS
    unread_count: int = 0
    # Roster-display denormalized fields (kept in sync on each message):
    last_message_at: str = ""
    last_message_preview: str = ""
    last_direction: str = ""             # "in" | "out"
    # Proactive scheduling (precomputed on each message event; the runner just polls these):
    timezone: str = ""                   # IANA tz; DEFAULT_TZ when blank
    next_proactive_at: str = ""          # UTC ISO — when the runner should act ("" = nothing due)
    next_proactive_kind: str = ""        # "nudge" | "template" | ""
    nudge_sent_for_window: str = ""      # window_open_until value a nudge was already sent for
    reengage_count: int = 0              # consecutive templates sent without a reply
    last_reengage_at: str = ""
    quiet_since: str = ""                # first time we found this contact gone quiet
    ai_state: dict[str, Any] = field(default_factory=dict)  # AI responder session state (phase, counters, history)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContactMeta":
        known = {f: d.get(f) for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)

    def is_in_window(self, at: Optional[datetime.datetime] = None) -> bool:
        """True if we can still send a free-form reply (< WINDOW_HOURS since last inbound)."""
        if not self.window_open_until:
            return False
        at = at or now_dt()
        return at < datetime.datetime.fromisoformat(self.window_open_until)


def window_open_until(last_inbound_iso: str) -> str:
    dt = datetime.datetime.fromisoformat(last_inbound_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return to_iso(dt + datetime.timedelta(hours=WINDOW_HOURS))


def parse_iso(s: str) -> datetime.datetime:
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _tz(name: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 - unknown tz falls back to the pilot default
        return zoneinfo.ZoneInfo(DEFAULT_TZ)


def _hhmm(s: str) -> datetime.time:
    h, m = s.split(":")
    return datetime.time(int(h), int(m))


def is_quiet(now: datetime.datetime, tz: zoneinfo.ZoneInfo,
             quiet_start: str = QUIET_START, quiet_end: str = QUIET_END) -> bool:
    """True if `now` (any tz) is inside the local quiet window, which crosses midnight."""
    lt = now.astimezone(tz).time()
    return lt >= _hhmm(quiet_start) or lt < _hhmm(quiet_end)


def last_social_before(dt: datetime.datetime, tz: zoneinfo.ZoneInfo,
                       quiet_start: str = QUIET_START) -> datetime.datetime:
    """Latest instant ≤ dt that is in social hours (just before quiet starts, minus a buffer)."""
    if not is_quiet(dt, tz):
        return dt
    local = dt.astimezone(tz)
    qs = _hhmm(quiet_start)
    day = local.date() if local.time() >= qs else local.date() - datetime.timedelta(days=1)
    boundary = datetime.datetime.combine(day, qs, tzinfo=tz) - PRE_QUIET_BUFFER
    return boundary.astimezone(datetime.timezone.utc)


def next_social_start(dt: datetime.datetime, tz: zoneinfo.ZoneInfo,
                      quiet_end: str = QUIET_END) -> datetime.datetime:
    """Earliest instant ≥ dt that is in social hours (quiet-end boundary)."""
    if not is_quiet(dt, tz):
        return dt
    local = dt.astimezone(tz)
    qe = _hhmm(quiet_end)
    day = local.date() if local.time() < qe else local.date() + datetime.timedelta(days=1)
    boundary = datetime.datetime.combine(day, qe, tzinfo=tz)
    return boundary.astimezone(datetime.timezone.utc)


def compute_next_proactive(meta: "ContactMeta", now: datetime.datetime) -> tuple[str, str]:
    """Precompute the next system-initiated send for a conversation → (iso_when, kind).

    Returns ("", "") when nothing is due (no consent, dormant, archived, or never messaged).
    Nudge = free-form, in-window, herded into social hours. Template = paid fallback once the
    window has actually lapsed. All returned times are already quiet-hours-safe.
    """
    if (meta.consent_state != "granted" or meta.status != "active"
            or not meta.keep_warm or not meta.window_open_until):
        return ("", "")   # no keep-warm: no consent, inactive, opted out, or never messaged

    tz = _tz(meta.timezone)
    expiry = parse_iso(meta.window_open_until)
    window_open = now < expiry
    already_nudged = meta.nudge_sent_for_window == meta.window_open_until

    # 1) Window still open and not yet nudged → schedule the anti-drift free-form nudge.
    if window_open and not already_nudged:
        if TEST_MODE and meta.last_inbound_at:   # fast, repeatable loop for observing keep-warm
            return (to_iso(parse_iso(meta.last_inbound_at) + TEST_LEAD), "nudge")
        target = expiry - NUDGE_LEAD
        nudge_at = last_social_before(min(target, expiry), tz)
        if nudge_at >= now:
            return (to_iso(nudge_at), "nudge")
        if not is_quiet(now, tz):
            return (to_iso(now), "nudge")   # social right now, window still open → nudge immediately
        # else: missed the social slot and it's quiet → fall through to the template fallback.

    # 2) Template re-engagement fallback (window lapsed, or nudge already spent this window).
    if meta.reengage_count >= MAX_TEMPLATE_MISSES:
        return ("", "")   # dormant — stop to protect the number's quality rating
    base = now if now > expiry else expiry
    when = next_social_start(base, tz)
    if meta.last_reengage_at:  # enforce cadence cap between templates
        floor = parse_iso(meta.last_reengage_at) + MIN_TEMPLATE_GAP
        if when < floor:
            when = next_social_start(floor, tz)
    return (to_iso(when), "template")


def _reschedule(meta: "ContactMeta", now: datetime.datetime) -> None:
    at, kind = compute_next_proactive(meta, now)
    meta.next_proactive_at = at
    meta.next_proactive_kind = kind


def _preview(msg: Message, limit: int = 80) -> str:
    if msg.text:
        t = msg.text.strip().replace("\n", " ")
        return t[:limit]
    return {"image": "📷 Photo", "audio": "🎤 Voice message"}.get(msg.type, "📎 Attachment")


# --------------------------------------------------------------------------- S3 client seam
class S3Like(Protocol):
    def put_object(self, **kw: Any) -> Any: ...
    def get_object(self, **kw: Any) -> Any: ...
    def list_objects_v2(self, **kw: Any) -> Any: ...


class ConversationStore:
    def __init__(self, s3: S3Like, bucket: str, prefix: str = PREFIX) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")

    # ------------------------------------------------------------------ keys
    def _meta_key(self, uid: str) -> str:
        return f"{self._prefix}/{uid}/meta.json"

    def _messages_prefix(self, uid: str) -> str:
        return f"{self._prefix}/{uid}/messages/"

    def _message_key(self, uid: str, msg: Message) -> str:
        return f"{self._messages_prefix(uid)}{msg.ts_ms:013d}-{msg.id}.json"

    # ------------------------------------------------------------------ low-level io
    def _get_json(self, key: str) -> Optional[dict[str, Any]]:
        try:
            body = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception as e:  # noqa: BLE001 - treat any "not found" as absent
            if "NoSuchKey" in type(e).__name__ or "NoSuchKey" in str(e) or "404" in str(e):
                return None
            raise
        return json.loads(body)

    def _put_json(self, key: str, obj: dict[str, Any]) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

    # ------------------------------------------------------------------ meta
    def get_meta(self, uid: str) -> Optional[ContactMeta]:
        d = self._get_json(self._meta_key(uid))
        return ContactMeta.from_dict(d) if d else None

    def put_meta(self, meta: ContactMeta) -> None:
        self._put_json(self._meta_key(meta.user_id), meta.to_dict())

    def ensure_contact(self, uid: str, phone: str) -> ContactMeta:
        meta = self.get_meta(uid)
        if meta is None:
            meta = ContactMeta(user_id=uid, phone=phone)
            self.put_meta(meta)
        elif phone and not meta.phone:
            meta.phone = phone
            self.put_meta(meta)
        return meta

    # ------------------------------------------------------------------ messages
    def append_message(self, uid: str, msg: Message) -> str:
        key = self._message_key(uid, msg)
        self._put_json(key, msg.to_dict())
        return key

    def list_messages(self, uid: str, since: Optional[str] = None, limit: int = 500) -> list[Message]:
        """Thread in chronological order. `since` is a cursor (a message key) for polling."""
        out: list[Message] = []
        for key in self._list_keys(self._messages_prefix(uid), start_after=since, limit=limit):
            d = self._get_json(key)
            if d:
                out.append(Message.from_dict(d))
        return out

    def _list_keys(self, prefix: str, start_after: Optional[str] = None,
                   limit: int = 1000) -> list[str]:
        keys: list[str] = []
        token: Optional[str] = None
        while True:
            kw: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix, "MaxKeys": 1000}
            if start_after:
                kw["StartAfter"] = start_after
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            for c in resp.get("Contents", []):
                keys.append(c["Key"])
                if len(keys) >= limit:
                    return keys
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return keys

    def latest_cursor(self, uid: str) -> Optional[str]:
        keys = self._list_keys(self._messages_prefix(uid))
        return keys[-1] if keys else None

    # ------------------------------------------------------------------ roster
    def list_conversation_ids(self) -> list[str]:
        ids: list[str] = []
        token: Optional[str] = None
        base = f"{self._prefix}/"
        while True:
            kw: dict[str, Any] = {"Bucket": self._bucket, "Prefix": base, "Delimiter": "/"}
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            for cp in resp.get("CommonPrefixes", []):
                ids.append(cp["Prefix"][len(base):].rstrip("/"))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return ids

    def list_roster(self) -> list[ContactMeta]:
        """All contacts, most-recent activity first. O(n) reads — fine at pilot scale."""
        metas = [self.get_meta(uid) for uid in self.list_conversation_ids()]
        rows = [m for m in metas if m is not None]
        rows.sort(key=lambda m: m.last_message_at or m.created_at, reverse=True)
        return rows

    # ------------------------------------------------------------------ high-level transitions
    def record_inbound(self, uid: str, phone: str, msg: Message) -> ContactMeta:
        """Persist an inbound message and advance conversation state (opens the 24h window)."""
        self.append_message(uid, msg)
        meta = self.get_meta(uid) or ContactMeta(user_id=uid, phone=phone)
        if phone and not meta.phone:
            meta.phone = phone
        meta.last_inbound_at = msg.at
        meta.window_open_until = window_open_until(msg.at)
        meta.unread_count += 1
        meta.last_message_at = msg.at
        meta.last_message_preview = _preview(msg)
        meta.last_direction = "in"
        # User re-engaged → fresh window: reset proactive state and reschedule from scratch.
        meta.nudge_sent_for_window = ""
        meta.reengage_count = 0
        meta.quiet_since = ""
        _reschedule(meta, parse_iso(msg.at))
        self.put_meta(meta)
        return meta

    def record_outbound(self, uid: str, msg: Message,
                        proactive_kind: Optional[str] = None,
                        ai_state: Optional[dict] = None,
                        locale: Optional[str] = None) -> ContactMeta:
        """Persist an outbound message and advance state (clears unread).

        proactive_kind marks a system-initiated send: "nudge" (spends the nudge for this window)
        or "template" (counts toward the cadence cap / dormancy). None = a reactive reply
        (operator or AI) — it does not consume the nudge and does not gate on quiet hours.
        ai_state, when given, is persisted alongside (the AI responder's session state).
        """
        self.append_message(uid, msg)
        meta = self.get_meta(uid) or ContactMeta(user_id=uid)
        meta.last_outbound_at = msg.at
        meta.unread_count = 0
        meta.last_message_at = msg.at
        meta.last_message_preview = _preview(msg)
        meta.last_direction = "out"
        if ai_state is not None:
            meta.ai_state = ai_state
        if locale:
            meta.locale = locale
        if proactive_kind == "nudge":
            meta.nudge_sent_for_window = meta.window_open_until
            meta.quiet_since = meta.quiet_since or msg.at
        elif proactive_kind == "template":
            meta.reengage_count += 1
            meta.last_reengage_at = msg.at
            meta.quiet_since = meta.quiet_since or msg.at
        _reschedule(meta, parse_iso(msg.at))
        self.put_meta(meta)
        return meta

    def list_due(self, now: datetime.datetime) -> list[ContactMeta]:
        """Contacts whose precomputed proactive send is due (≤ now). The 5-min runner's query."""
        due = []
        for m in self.list_roster():
            if m.next_proactive_kind and m.next_proactive_at and parse_iso(m.next_proactive_at) <= now:
                due.append(m)
        return due

    def mark_read(self, uid: str) -> Optional[ContactMeta]:
        meta = self.get_meta(uid)
        if meta and meta.unread_count:
            meta.unread_count = 0
            self.put_meta(meta)
        return meta

    def set_keep_warm(self, uid: str, enabled: bool,
                      now: Optional[datetime.datetime] = None) -> Optional[ContactMeta]:
        """Operator toggle: enable/disable proactive keep-warm for one number and reschedule."""
        meta = self.get_meta(uid)
        if meta is None:
            return None
        meta.keep_warm = bool(enabled)
        _reschedule(meta, now or now_dt())   # disabling clears next_proactive_*; enabling recomputes
        self.put_meta(meta)
        return meta


def new_message_id() -> str:
    return uuid.uuid4().hex
