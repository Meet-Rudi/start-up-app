"""
MEET_RUDI platform — the clock a clone reads by.

Ported from services/whatsapp/src/timeline.py on 2026-10-28; the WhatsApp and call services keep
their own copies until they are cut over (conventions §6).

Every moment is kept as a full UTC timestamp and spoken about the way a person would: "today by
18:00", "last Tuesday". The model never works a date out; it cannot reliably know what time it is,
and a memory reading "walk at 7PM" with no date is how a check-in once asked about an evening that
had not happened yet. The arithmetic happens here and the model gets labels that are already right.

  now_note()        the [Now: ...] line every prompt carries
  humanize() due()  a stored timestamp as a human phrase, resolved against now
  render()          chat history for the model: stamps become time markers, `at` is stripped

Platform change: no environment variables. The time zone is the end-user's (or the clone's
default), passed by the caller; DEFAULT_TZ is only the last resort for an unreadable zone.
"""

from __future__ import annotations

import datetime
import re
import zoneinfo

DEFAULT_TZ = "Europe/Brussels"

# A marker is only worth its tokens once time has moved: after a two-hour silence "earlier today"
# matters, and across midnight it is the whole point.
GAP = datetime.timedelta(hours=2)

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
)  # fmt: skip

MARKER_RE = re.compile(r"\[Time marker:[^\]]*\]\s*")

# English on purpose: an instruction to the model, never a string a person reads.
SPEAK_TIMES = (
    'When you mention a time to the person, say it the way a friend would — "today by 18:00", '
    '"tomorrow morning", "last Tuesday" — never a date stamp or a timestamp, and never repeat '
    "a [Time marker] line. Before asking how something went, check that it has actually happened: "
    "if the time they planned is still ahead, encourage them for it instead of asking for an "
    "update."
)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def to_iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.UTC).isoformat()


def stamp(now: datetime.datetime | None = None) -> str:
    """The timestamp stored with a remembered moment. Always UTC, always complete."""
    return to_iso(now or now_utc())


def parse(value: object) -> datetime.datetime | None:
    """A stored timestamp back to an aware datetime; None for anything unreadable or missing."""
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(value or ""))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.UTC)


def zone(name: str = "", default: str = DEFAULT_TZ) -> zoneinfo.ZoneInfo:
    """The named zone; `default` when the name is empty or unknown (a bad zone never breaks a turn)."""
    try:
        return zoneinfo.ZoneInfo(name or default)
    except Exception:  # noqa: BLE001
        return zoneinfo.ZoneInfo(DEFAULT_TZ)


def _date_words(local: datetime.datetime, now_local: datetime.datetime) -> str:
    text = f"{_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}"
    return text if local.year == now_local.year else f"{text} {local.year}"


def _span(delta: datetime.timedelta) -> str:
    minutes = int(abs(delta.total_seconds()) // 60)
    if minutes < 90:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = round(minutes / 60)
    if hours < 36:
        return f"{hours} hours"
    return f"{round(hours / 24)} days"


def humanize(at: object, now: datetime.datetime | None = None, tz: str = "") -> str:
    """A stored moment the way a person would place it, with its date alongside.

    "yesterday at 19:00 (Monday 15 September)": the relative half is what to say; the date half
    lets a pause of several days be reasoned about. "" if unreadable.
    """
    moment = parse(at)
    if moment is None:
        return ""
    z = zone(tz)
    now_local = (now or now_utc()).astimezone(z)
    local = moment.astimezone(z)
    days = (local.date() - now_local.date()).days
    if days == 0:
        word = "today"
    elif days == -1:
        word = "yesterday"
    elif days == 1:
        word = "tomorrow"
    elif -6 <= days < 0:
        word = f"last {_DAYS[local.weekday()]}"
    elif 0 < days <= 6:
        word = f"this coming {_DAYS[local.weekday()]}"
    else:
        word = f"{abs(days)} days {'ago' if days < 0 else 'from now'}"
    return f"{word} at {local.strftime('%H:%M')} ({_date_words(local, now_local)})"


def due(at: object, now: datetime.datetime | None = None, tz: str = "") -> str:
    """A planned moment, and whether it has come yet."""
    moment = parse(at)
    if moment is None:
        return ""
    now = now or now_utc()
    label = humanize(moment, now, tz)
    if moment > now:
        return f"{label} — still to come, in {_span(moment - now)}"
    return f"{label} — already passed, {_span(now - moment)} ago"


def after_minutes(
    minutes: object, now: datetime.datetime | None = None, limit_hours: int = 24
) -> str:
    """Stamp for "in N minutes", as the model reports it. "" when absent or out of range."""
    try:
        value = int(minutes)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return ""
    if value <= 0 or value > limit_hours * 60:
        return ""
    return stamp((now or now_utc()) + datetime.timedelta(minutes=value))


def now_note(
    now: datetime.datetime | None = None, tz: str = "", speaking: bool = True
) -> str:
    """The line that gives the model a clock; goes on every prompt, on every channel.

    `speaking=False` drops the how-to-say-it guidance for output that is stored rather than said:
    a summary must keep full dates.
    """
    z = zone(tz)
    local = (now or now_utc()).astimezone(z)
    moment = (
        f"{_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]} {local.year}, "
        f"{local.strftime('%H:%M')} in {z.key}"
    )
    guidance = (" " + SPEAK_TIMES) if speaking else ""
    return (
        f"[Now: it is {moment}. Every time in these notes is already worked out against this "
        'moment — trust labels like "yesterday", "still to come" or "already passed" rather '
        f"than calculating dates yourself.{guidance}]"
    )


def render(
    history: list[dict] | None,
    now: datetime.datetime | None = None,
    tz: str = "",
    gap: datetime.timedelta = GAP,
) -> list[dict]:
    """History as the model should see it: role and content only, marked where time moved.

    The marker goes inside the message text, not as a system message between turns: several
    chat templates served through vLLM refuse any system message that is not the first, and
    those are exactly the providers a cascade fails over to.
    """
    now = now or now_utc()
    z = zone(tz)
    out: list[dict] = []
    prev: datetime.datetime | None = None
    for entry in history or []:
        content = entry.get("content", "")
        moment = parse(entry.get("at")) if entry.get("at") else None
        if moment is not None:
            if prev is None:
                moved = now - moment >= gap
            else:
                moved = (
                    moment - prev >= gap
                    or moment.astimezone(z).date() != prev.astimezone(z).date()
                )
            if moved:
                content = f"[Time marker: sent {humanize(moment, now, tz)}]\n{content}"
            prev = moment
        out.append({"role": entry.get("role", "user"), "content": content})
    return out


def strip_markers(text: str) -> str:
    """A marker the model copied into its reply never reaches the person."""
    original = text or ""
    cleaned = MARKER_RE.sub("", original)
    return cleaned.strip() if cleaned != original else original
