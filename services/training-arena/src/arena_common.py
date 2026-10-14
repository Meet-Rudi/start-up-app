"""
MEET_RUDI Training Arena — small shared helpers: time, IDs, names, S3 JSON, logs.

Everything the arena stores lives in its own bucket (ARENA_BUCKET). Nothing here touches the live
data bucket. Logs are structured lines with opaque IDs, event names, timings and costs — never
message text (CLAUDE.md §5).
"""

import os
import re
import json
import math
import random
import hashlib
import datetime
import unicodedata
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("ARENA_TZ", "Europe/Brussels"))
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"          # no I, L, O, U
_RANDOM = random.SystemRandom()
CHARS_PER_TOKEN = 3.2                                    # deliberately over-counts EN / NL tokens


# --------------------------------------------------------------------------- time
def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt=None):
    """ISO-8601 UTC with milliseconds and a Z: 2026-10-13T08:14:02.123Z."""
    dt = (dt or now_utc()).astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def local(dt=None):
    return (dt or now_utc()).astimezone(TZ)


def display(value):
    """"2026-10-13 10:14" on the team's (Brussels) clock, for the console. "" for nothing."""
    dt = parse_iso(value) if isinstance(value, str) else value
    return local(dt).strftime("%Y-%m-%d %H:%M") if dt else ""


def local_day(dt=None):
    return local(dt).strftime("%Y-%m-%d")


def in_quiet_hours(dt=None, start="21:30", end="06:30"):
    t = local(dt).strftime("%H:%M")
    return t >= start or t < end


# --------------------------------------------------------------------------- IDs and names
def new_code(n=4):
    return "".join(_RANDOM.choice(CROCKFORD) for _ in range(n))


def new_batch_id(dt=None, code=None):
    """TA-<yyyymmdd>-<XXXX>; the date is the submission day on the Brussels calendar."""
    return "TA-%s-%s" % (local(dt).strftime("%Y%m%d"), code or new_code(4))


BATCH_ID_RE = re.compile(r"^TA-\d{8}-[0-9A-HJKMNP-TV-Z]{4}$")
CASE_ID_RE = re.compile(r"^(TA-\d{8}-[0-9A-HJKMNP-TV-Z]{4})-(\d{4,6})$")


def case_width(total):
    return max(4, len(str(int(total or 0))))


def case_id(batch_id, seq, total):
    return "%s-%0*d" % (batch_id, case_width(total), int(seq))


def split_case_id(cid):
    m = CASE_ID_RE.match(str(cid or ""))
    return (m.group(1), int(m.group(2))) if m else (None, None)


def slug(name, limit=40):
    """"Goal setting smoke!" → "goal-setting-smoke"."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:limit].rstrip("-")


def file_stem(batch_id, name=""):
    return batch_id + ("__" + name if name else "")


def results_name(stem, index, total, partial_at=None):
    width = 2 if total <= 99 else 3
    kind = "results" if not partial_at else "partial-results-" + partial_at
    return "%s__%s__p%0*dof%0*d.yaml" % (stem, kind, width, index, width, total)


def est_tokens(text_or_len):
    n = text_or_len if isinstance(text_or_len, int) else len(text_or_len or "")
    return int(math.ceil(n / CHARS_PER_TOKEN))


def sha256_hex(data):
    return hashlib.sha256(data if isinstance(data, bytes) else str(data).encode("utf-8")).hexdigest()


def round_eur(v):
    return round(float(v or 0.0), 6)


# --------------------------------------------------------------------------- S3
class Bucket:
    """JSON / text objects in one bucket. The s3 client is injected so tests pass a fake."""

    def __init__(self, s3, name):
        self.s3, self.name = s3, name

    def get_bytes(self, key):
        return self.s3.get_object(Bucket=self.name, Key=key)["Body"].read()

    def get_text(self, key, default=None):
        try:
            return self.get_bytes(key).decode("utf-8")
        except Exception:  # noqa: BLE001 - missing object reads as the default
            return default

    def get_json(self, key, default=None):
        raw = self.get_text(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def put_json(self, key, obj):
        self.s3.put_object(Bucket=self.name, Key=key, ContentType="application/json",
                           Body=json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def put_text(self, key, text, content_type="text/plain; charset=utf-8"):
        self.s3.put_object(Bucket=self.name, Key=key, ContentType=content_type,
                           Body=text.encode("utf-8") if isinstance(text, str) else text)

    def delete(self, key):
        try:
            self.s3.delete_object(Bucket=self.name, Key=key)
        except Exception:  # noqa: BLE001 - deleting what is gone is fine
            pass

    def list_keys(self, prefix, start_after="", limit=None):
        keys, token = [], None
        while True:
            kw = {"Bucket": self.name, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            elif start_after:
                kw["StartAfter"] = start_after
            resp = self.s3.list_objects_v2(**kw)
            keys += [o["Key"] for o in resp.get("Contents", [])]
            if limit and len(keys) >= limit:
                return keys[:limit]
            if not resp.get("IsTruncated"):
                return keys
            token = resp.get("NextContinuationToken")

    def exists(self, key):
        try:
            self.s3.get_object(Bucket=self.name, Key=key)
            return True
        except Exception:  # noqa: BLE001
            return False


# --------------------------------------------------------------------------- logs
def log(event, **fields):
    """One structured line. Callers pass IDs, counts, timings and costs — never message text."""
    fields["event"] = event
    print(json.dumps(fields, ensure_ascii=False, default=str, separators=(",", ":")))
