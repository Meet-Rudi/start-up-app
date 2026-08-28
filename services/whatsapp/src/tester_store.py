"""
MEET_RUDI — TesterStore (S3-backed system of record for the third-party tester console).

The storage seam for the closed tester cohort. `tester_api` is the only caller; the layout stays
swappable behind these methods, exactly as `ConversationStore` does for WhatsApp conversations.

Layout (all under the same EU-region data bucket):

    testers/{testerId}/profile.json               # tester record: PII, credentials, ledger
    testers/{testerId}/feedback/{track}.json      # one object per track (chat|call|whatsapp)
    tester-console/sessions/{sha256(token)}.json  # live sessions (idle-expiring)
    tester-console/tokens/{sha256(token)}.json    # single-use verify / reset links
    tester-console/settings.json                  # registration open/closed, daily call cap
    tester-console/call-queue.json                # who is waiting, who is on the line
    tester-console/call-day/{YYYY-MM-DD}.json     # cohort-wide daily call counter (quota)

Design choices, mirroring store.py:
- `testerId` is HMAC(salt, lowercased email) — deterministic, so a re-submitted registration
  updates one record instead of forking a second. No name/email/phone in any S3 key (§5).
- Raw PII (name, email, phone) lives ONLY inside profile.json, which never leaves the AWS-EU
  plane, and is never logged.
- Tokens and session tokens are stored as SHA-256 digests. A leaked bucket listing therefore
  yields no usable credential, and neither does an object read.
- Passwords are PBKDF2-HMAC-SHA256 with a per-tester random salt. Never reversible, never logged.
- Pure stdlib + an injected S3 client (boto3 at runtime, an in-memory fake in tests).
"""

from __future__ import annotations

import os
import json
import hmac
import base64
import hashlib
import secrets
import datetime
from dataclasses import dataclass, field, asdict
from typing import Any, Optional, Protocol

import store  # iso_now / now_dt / is_quiet / _tz — one clock and one quiet-hours rule, not two

TESTER_PREFIX = "testers"
CONSOLE_PREFIX = "tester-console"

TRACKS = ("chat", "call", "whatsapp")

# The four call goals. Rudi always receives one as an input parameter; the tester never sees it
# and can never choose it (product decision). Assigned round-robin at registration so a cohort
# exercises all four, and changeable by an admin.
CALL_GOALS = ("GET_TO_KNOW", "SET_NEARTERM_GOAL", "GOAL_FOLLOWUP", "REINSTATE_TALK")

# Fallback when the tester leaves the free-text goal blank (product decision).
DEFAULT_GOAL = "Improve my lifestyle to boost my medical condition"

LOCALES = ("nl-BE", "en")
DEFAULT_LOCALE = "nl-BE"

MAX_CALLS_PER_TESTER = int(os.environ.get("TESTER_MAX_CALLS", "5"))
MAX_FAILED_LOGINS = int(os.environ.get("TESTER_MAX_FAILED_LOGINS", "10"))
SESSION_IDLE_MINUTES = int(os.environ.get("TESTER_SESSION_IDLE_MIN", "10"))
LINK_TTL_HOURS = int(os.environ.get("TESTER_LINK_TTL_HOURS", "24"))
DAILY_CALL_CAP = int(os.environ.get("TESTER_DAILY_CALL_CAP", "40"))

PBKDF2_ROUNDS = int(os.environ.get("TESTER_PBKDF2_ROUNDS", "600000"))


# --------------------------------------------------------------------------- identity helpers
def tester_id(email: str, salt: str) -> str:
    """Deterministic pseudonymous id. Same email → same record, and no PII in the key."""
    digest = hmac.new(salt.encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()
    return "tst_" + digest[:24]


def new_token() -> str:
    """A URL-safe secret for a session or an email link. Returned once, stored only as a digest."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """PBKDF2-HMAC-SHA256. Encoded as `pbkdf2_sha256$rounds$salt_b64$hash_b64`."""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check. A malformed or absent hash is a failure, never an exception."""
    try:
        scheme, rounds, salt_b64, hash_b64 = (encoded or "").split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 base64.b64decode(salt_b64), int(rounds))
        return hmac.compare_digest(base64.b64encode(dk).decode(), hash_b64)
    except (ValueError, TypeError):
        return False


def call_goal_for(index: int) -> str:
    """Round-robin assignment, so a cohort of any size exercises all four goals evenly."""
    return CALL_GOALS[index % len(CALL_GOALS)]


# --------------------------------------------------------------------------- the record
@dataclass
class Tester:
    tester_id: str
    # --- PII: AWS-EU plane only, never in keys or logs ---
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""                       # E.164, the ONLY number Rudi may call or message
    # --- profile ---
    locale: str = DEFAULT_LOCALE          # nl-BE | en — drives console, mails, Rudi's voice
    help_areas: list[str] = field(default_factory=list)
    goal: str = DEFAULT_GOAL
    call_goal: str = CALL_GOALS[0]        # system-set, never shown to the tester
    wa_user_id: str = ""                  # store.user_id(phone) — links the WhatsApp thread
    consent_health: bool = False
    consent_recording: bool = False
    whatsapp_confirmed: bool = False
    # --- lifecycle ---
    status: str = "pending"               # pending | active | locked | revoked
    created_at: str = field(default_factory=store.iso_now)
    verified_at: str = ""
    password_hash: str = ""
    failed_logins: int = 0
    locked_at: str = ""
    last_login_at: str = ""
    last_ack_at: str = ""                 # do's-and-don'ts acknowledgement (per session)
    # --- call ledger. Only CONNECTED calls are deducted (product decision) ---
    calls_used: int = 0
    calls_max: int = MAX_CALLS_PER_TESTER
    last_call_at: str = ""
    last_call_outcome: str = ""           # connected | voicemail | no_answer | failed
    # --- track progress: not_started | in_progress | done ---
    track_chat: str = "not_started"
    track_call: str = "not_started"
    track_whatsapp: str = "not_started"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Tester":
        known = {f: d.get(f) for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)

    def calls_left(self) -> int:
        return max(0, self.calls_max - self.calls_used)

    def public(self) -> dict[str, Any]:
        """What the tester's own browser may see. Deliberately omits `call_goal` — the assigned
        goal is invisible to them by design — and every credential field."""
        return {
            "tester_id": self.tester_id,
            "first_name": self.first_name,
            "initials": ((self.first_name[:1] + self.last_name[:1]) or "?").upper(),
            "phone": self.phone,
            "locale": self.locale,
            "goal": self.goal,
            "help_areas": self.help_areas,
            "calls_used": self.calls_used,
            "calls_max": self.calls_max,
            "calls_left": self.calls_left(),
            "last_call_outcome": self.last_call_outcome,
            "tracks": {"chat": self.track_chat, "call": self.track_call,
                       "whatsapp": self.track_whatsapp},
        }

    def admin_row(self) -> dict[str, Any]:
        """What the test team sees. Phone is masked even here — nobody needs the full number to
        run the cohort, and this payload crosses the network to a browser."""
        return {
            "tester_id": self.tester_id,
            "name": ("%s %s." % (self.first_name, self.last_name[:1])).strip(),
            "email": self.email,
            "phone_masked": mask_phone(self.phone),
            "locale": self.locale,
            "status": self.status,
            "call_goal": self.call_goal,
            "calls_used": self.calls_used,
            "calls_max": self.calls_max,
            "created_at": self.created_at,
            "verified_at": self.verified_at,
            "last_login_at": self.last_login_at,
            "last_call_at": self.last_call_at,
            "last_call_outcome": self.last_call_outcome,
            "failed_logins": self.failed_logins,
            "tracks": {"chat": self.track_chat, "call": self.track_call,
                       "whatsapp": self.track_whatsapp},
        }


def mask_phone(phone: str) -> str:
    """+32479123456 → "+32 479 ••• •56".

    Enough for the test team to recognise a row, never enough to dial it. Grouped the Belgian
    way (country, operator, then masked) because a run of digits with holes in it is hard to
    match against anything by eye, which defeats the point of showing it at all.
    """
    if not phone or len(phone) < 8:
        return ""
    return "%s %s ••• •%s" % (phone[:3], phone[3:6], phone[-2:])


# --------------------------------------------------------------------------- S3 seam
class S3Like(Protocol):
    def put_object(self, **kw: Any) -> Any: ...
    def get_object(self, **kw: Any) -> Any: ...
    def list_objects_v2(self, **kw: Any) -> Any: ...
    def delete_object(self, **kw: Any) -> Any: ...


class TesterStore:
    def __init__(self, s3: S3Like, bucket: str,
                 prefix: str = TESTER_PREFIX, console_prefix: str = CONSOLE_PREFIX) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.console = console_prefix.strip("/")

    # ---------------------------------------------------------------- raw json
    def _get_json(self, key: str) -> Optional[dict[str, Any]]:
        try:
            raw = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception:  # noqa: BLE001 - absent or unreadable both mean "nothing on record"
            return None
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return obj if isinstance(obj, dict) else None

    def _put_json(self, key: str, obj: dict[str, Any]) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key,
                           Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           ContentType="application/json")

    def _delete(self, key: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except Exception:  # noqa: BLE001 - already gone is the desired end state
            pass

    def _list_keys(self, prefix: str, limit: int = 1000) -> list[str]:
        keys: list[str] = []
        token: Optional[str] = None
        while True:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            resp = self.s3.list_objects_v2(**kw)
            for item in resp.get("Contents", []) or []:
                keys.append(item["Key"])
                if len(keys) >= limit:
                    return keys
            if not resp.get("IsTruncated"):
                return keys
            token = resp.get("NextContinuationToken")
            if not token:
                return keys

    # ---------------------------------------------------------------- testers
    def _profile_key(self, tid: str) -> str:
        return "%s/%s/profile.json" % (self.prefix, tid)

    def get(self, tid: str) -> Optional[Tester]:
        obj = self._get_json(self._profile_key(tid))
        return Tester.from_dict(obj) if obj else None

    def put(self, tester: Tester) -> Tester:
        self._put_json(self._profile_key(tester.tester_id), tester.to_dict())
        return tester

    def list_all(self) -> list[Tester]:
        """Roster for the admin pane. S3-only, like the WhatsApp roster — fine at cohort scale."""
        out: list[Tester] = []
        for key in self._list_keys(self.prefix + "/"):
            if key.endswith("/profile.json"):
                obj = self._get_json(key)
                if obj:
                    out.append(Tester.from_dict(obj))
        out.sort(key=lambda t: t.created_at or "")
        return out

    def count(self) -> int:
        return sum(1 for k in self._list_keys(self.prefix + "/") if k.endswith("/profile.json"))

    # ---------------------------------------------------------------- email links (verify/reset)
    def _token_key(self, digest: str) -> str:
        return "%s/tokens/%s.json" % (self.console, digest)

    def issue_link(self, tid: str, kind: str, ttl_hours: int = LINK_TTL_HOURS) -> str:
        """Mint a single-use link token. Returns the clear token; only its digest is stored."""
        token = new_token()
        expires = store.now_dt() + datetime.timedelta(hours=ttl_hours)
        self._put_json(self._token_key(token_digest(token)), {
            "kind": kind, "tester_id": tid,
            "created_at": store.iso_now(), "expires_at": store.to_iso(expires), "used_at": "",
        })
        return token

    def consume_link(self, token: str, kind: str) -> tuple[Optional[str], str]:
        """Validate and burn a link token. Returns (tester_id, "") or (None, reason).

        Single-use is enforced by deleting the object, so a replayed link cannot be redeemed even
        if the attacker races the first redemption.
        """
        key = self._token_key(token_digest(token or ""))
        rec = self._get_json(key)
        if not rec:
            return None, "invalid"
        if rec.get("kind") != kind:
            return None, "invalid"
        expires = rec.get("expires_at") or ""
        if not expires or store.now_dt() >= store.parse_iso(expires):
            self._delete(key)
            return None, "expired"
        self._delete(key)
        return rec.get("tester_id") or None, ""

    def revoke_links(self, tid: str, kind: str = "") -> int:
        """Drop outstanding links for a tester — used when an admin re-issues one."""
        dropped = 0
        for key in self._list_keys("%s/tokens/" % self.console):
            rec = self._get_json(key)
            if rec and rec.get("tester_id") == tid and (not kind or rec.get("kind") == kind):
                self._delete(key)
                dropped += 1
        return dropped

    # ---------------------------------------------------------------- sessions
    def _session_key(self, digest: str) -> str:
        return "%s/sessions/%s.json" % (self.console, digest)

    def open_session(self, tid: str, role: str = "tester") -> str:
        token = new_token()
        self._put_json(self._session_key(token_digest(token)), {
            "tester_id": tid, "role": role,
            "created_at": store.iso_now(), "last_seen_at": store.iso_now(), "acked_at": "",
        })
        return token

    def touch_session(self, token: str) -> tuple[Optional[dict[str, Any]], str]:
        """Validate a session and slide its idle window. Returns (session, "") or (None, reason).

        Idle expiry is enforced HERE, server-side, on every request — a browser that keeps its
        token after the timeout still gets nothing (§0.5: never trust the client).
        """
        key = self._session_key(token_digest(token or ""))
        sess = self._get_json(key)
        if not sess:
            return None, "unauthorized"
        last = sess.get("last_seen_at") or sess.get("created_at") or ""
        if not last:
            self._delete(key)
            return None, "expired"
        idle = store.now_dt() - store.parse_iso(last)
        if idle > datetime.timedelta(minutes=SESSION_IDLE_MINUTES):
            self._delete(key)
            return None, "expired"
        sess["last_seen_at"] = store.iso_now()
        self._put_json(key, sess)
        return sess, ""

    def ack_session(self, token: str) -> None:
        """Record the do's-and-don'ts acknowledgement. Per session, so it shows on every entry."""
        key = self._session_key(token_digest(token or ""))
        sess = self._get_json(key)
        if sess is not None:
            sess["acked_at"] = store.iso_now()
            self._put_json(key, sess)

    def close_session(self, token: str) -> None:
        self._delete(self._session_key(token_digest(token or "")))

    def kill_sessions(self, tid: str) -> int:
        """Admin lever: drop every live session for a tester (misbehaviour, or a revoke)."""
        dropped = 0
        for key in self._list_keys("%s/sessions/" % self.console):
            sess = self._get_json(key)
            if sess and sess.get("tester_id") == tid:
                self._delete(key)
                dropped += 1
        return dropped

    def active_sessions(self) -> int:
        live = 0
        cutoff = store.now_dt() - datetime.timedelta(minutes=SESSION_IDLE_MINUTES)
        for key in self._list_keys("%s/sessions/" % self.console):
            sess = self._get_json(key)
            last = (sess or {}).get("last_seen_at") or ""
            if last and store.parse_iso(last) > cutoff:
                live += 1
        return live

    # ---------------------------------------------------------------- settings
    def _settings_key(self) -> str:
        return "%s/settings.json" % self.console

    def settings(self) -> dict[str, Any]:
        base = {"registration_open": True, "calling_paused": False,
                "daily_call_cap": DAILY_CALL_CAP, "invited_count": 0}
        base.update(self._get_json(self._settings_key()) or {})
        return base

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        merged = self.settings()
        merged.update(patch)
        self._put_json(self._settings_key(), merged)
        return merged

    # ---------------------------------------------------------------- feedback
    def _feedback_key(self, tid: str, track: str) -> str:
        return "%s/%s/feedback/%s.json" % (self.prefix, tid, track)

    def save_feedback(self, tid: str, track: str, score: Optional[int], text: str) -> dict[str, Any]:
        """Latest answer wins; earlier ones are kept in `history` so nothing a tester said is lost."""
        key = self._feedback_key(tid, track)
        prev = self._get_json(key) or {}
        history = list(prev.get("history") or [])
        if prev.get("sent_at"):
            history.append({k: prev.get(k) for k in ("score", "text", "sent_at")})
        rec = {"tester_id": tid, "track": track, "score": score, "text": text,
               "sent_at": store.iso_now(), "history": history}
        self._put_json(key, rec)
        return rec

    def get_feedback(self, tid: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for track in TRACKS:
            rec = self._get_json(self._feedback_key(tid, track))
            if rec:
                out[track] = {"score": rec.get("score"), "text": rec.get("text"),
                              "sent_at": rec.get("sent_at")}
        return out

    def feedback_count(self) -> int:
        return sum(1 for k in self._list_keys(self.prefix + "/") if "/feedback/" in k)

    # ---------------------------------------------------------------- call quota (cohort/day)
    def _day_key(self, day: str) -> str:
        return "%s/call-day/%s.json" % (self.console, day)

    def calls_today(self, day: str = "") -> int:
        day = day or store.iso_now()[:10]
        return int((self._get_json(self._day_key(day)) or {}).get("count", 0))

    def bump_calls_today(self, day: str = "") -> int:
        day = day or store.iso_now()[:10]
        key = self._day_key(day)
        rec = self._get_json(key) or {"day": day, "count": 0}
        rec["count"] = int(rec.get("count", 0)) + 1
        self._put_json(key, rec)
        return rec["count"]

    # ---------------------------------------------------------------- call queue
    def _queue_key(self) -> str:
        return "%s/call-queue.json" % self.console

    def queue(self) -> dict[str, Any]:
        base = {"active": None, "waiting": []}
        base.update(self._get_json(self._queue_key()) or {})
        return base

    def _save_queue(self, q: dict[str, Any]) -> None:
        self._put_json(self._queue_key(), q)

    def enqueue(self, tid: str) -> dict[str, Any]:
        """Join the line. Idempotent: asking twice keeps your original place, it doesn't add one.

        One call runs at a time so no tester gets a degraded line; the tester-facing copy blames
        demand rather than explaining our capacity (product decision).
        """
        q = self.queue()
        active = q.get("active") or {}
        if active.get("tester_id") == tid:
            return q
        waiting = [w for w in q.get("waiting", []) if w.get("tester_id") != tid]
        if not active:
            q["active"] = {"tester_id": tid, "since": store.iso_now(), "call_id": ""}
            q["waiting"] = waiting
        else:
            waiting.append({"tester_id": tid, "since": store.iso_now()})
            q["waiting"] = waiting
        self._save_queue(q)
        return q

    def position(self, tid: str) -> int:
        """0 = on the line now, 1..n = place in the queue, -1 = not queued."""
        q = self.queue()
        if (q.get("active") or {}).get("tester_id") == tid:
            return 0
        for i, w in enumerate(q.get("waiting", []), start=1):
            if w.get("tester_id") == tid:
                return i
        return -1

    def set_active_call(self, tid: str, call_id: str) -> None:
        q = self.queue()
        if (q.get("active") or {}).get("tester_id") == tid:
            q["active"]["call_id"] = call_id
            self._save_queue(q)

    def dequeue(self, tid: str) -> dict[str, Any]:
        """Leave the line (finished, failed, or gave up) and promote whoever is next."""
        q = self.queue()
        if (q.get("active") or {}).get("tester_id") == tid:
            waiting = list(q.get("waiting", []))
            q["active"] = ({"tester_id": waiting[0]["tester_id"], "since": store.iso_now(),
                            "call_id": ""} if waiting else None)
            q["waiting"] = waiting[1:] if waiting else []
        else:
            q["waiting"] = [w for w in q.get("waiting", []) if w.get("tester_id") != tid]
        self._save_queue(q)
        return q

    def clear_queue(self) -> None:
        self._save_queue({"active": None, "waiting": []})
