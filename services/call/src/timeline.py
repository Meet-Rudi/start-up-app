"""
MEET_RUDI — the clock Rudi reads by.

Rudi keeps every moment as a full UTC timestamp and talks about it the way a person would:
"today by 18:00", "yesterday evening", "last Tuesday". The model itself never works a date out.
It cannot reliably know what time it is, and a memory reading "walk at 7PM" with no date on it is
how Rudi once chased someone for an update on an evening that had not happened yet. So the
arithmetic happens here, and the model is handed labels that are already right.

Three jobs:
  now_note()        the [Now: ...] line every prompt carries
  humanize() due()  a stored timestamp as a human phrase, resolved against now
  render()          chat history for the model: stamps become time markers, `at` is stripped

Pure stdlib. The call service carries an identical copy (services/call/src/timeline.py) because
each service ships self-contained; a test there fails the moment the two copies drift apart.
"""

import os
import re
import datetime
import zoneinfo
from typing import Optional

DEFAULT_TZ = os.environ.get("DEFAULT_TZ", "Europe/Brussels")

# A marker is only worth its tokens once time has actually moved. Inside a live exchange the
# [Now] line is enough; after a two-hour silence "earlier today" starts to matter, and across
# midnight it is the whole point.
GAP = datetime.timedelta(hours=2)

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")

MARKER_RE = re.compile(r"\[Time marker:[^\]]*\]\s*")

# English on purpose: this is an instruction to the model, never a string the person reads. The
# model says the time in the person's own language.
SPEAK_TIMES = (
    "When you mention a time to the person, say it the way a friend would — \"today by 18:00\", "
    "\"tomorrow morning\", \"last Tuesday\" — never a date stamp or a timestamp, and never repeat "
    "a [Time marker] line. Before asking how something went, check that it has actually happened: "
    "if the time they planned is still ahead, encourage them for it instead of asking for an "
    "update."
)


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def to_iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat()


def stamp(now: Optional[datetime.datetime] = None) -> str:
    """The timestamp stored with a remembered moment. Always UTC, always complete."""
    return to_iso(now or now_utc())


def parse(value) -> Optional[datetime.datetime]:
    """A stored timestamp back to an aware datetime; None for anything unreadable or missing."""
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(value or ""))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def zone(name: str = "") -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name or DEFAULT_TZ)
    except Exception:  # noqa: BLE001 - a bad per-contact tz must not break a turn
        return zoneinfo.ZoneInfo(DEFAULT_TZ)


def _date_words(local: datetime.datetime, now_local: datetime.datetime) -> str:
    text = "%s %d %s" % (_DAYS[local.weekday()], local.day, _MONTHS[local.month - 1])
    return text if local.year == now_local.year else "%s %d" % (text, local.year)


def _span(delta: datetime.timedelta) -> str:
    minutes = int(abs(delta.total_seconds()) // 60)
    if minutes < 90:
        return "%d minute%s" % (minutes, "" if minutes == 1 else "s")
    hours = round(minutes / 60)
    if hours < 36:
        return "%d hours" % hours
    return "%d days" % round(hours / 24)


def humanize(at, now: Optional[datetime.datetime] = None, tz: str = "") -> str:
    """A stored moment the way a person would place it, with its date kept alongside.

    "yesterday at 19:00 (Monday 15 September)". The relative half is what Rudi should say; the
    date half is there so a pause of several days can still be reasoned about. "" if unreadable.
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
        word = "last %s" % _DAYS[local.weekday()]
    elif 0 < days <= 6:
        word = "this coming %s" % _DAYS[local.weekday()]
    else:
        word = "%d days %s" % (abs(days), "ago" if days < 0 else "from now")
    return "%s at %s (%s)" % (word, local.strftime("%H:%M"), _date_words(local, now_local))


def due(at, now: Optional[datetime.datetime] = None, tz: str = "") -> str:
    """A planned moment, and whether it has come yet — the exact question the model got wrong."""
    moment = parse(at)
    if moment is None:
        return ""
    now = now or now_utc()
    label = humanize(moment, now, tz)
    if moment > now:
        return "%s — still to come, in %s" % (label, _span(moment - now))
    return "%s — already passed, %s ago" % (label, _span(now - moment))


def after_minutes(minutes, now: Optional[datetime.datetime] = None, limit_hours: int = 24) -> str:
    """Stamp for "in N minutes", as the model reports it. "" when absent or out of range."""
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return ""
    if value <= 0 or value > limit_hours * 60:
        return ""
    return stamp((now or now_utc()) + datetime.timedelta(minutes=value))


def now_note(now: Optional[datetime.datetime] = None, tz: str = "", speaking: bool = True) -> str:
    """The line that gives Rudi a clock. Goes on every prompt, on every channel.

    `speaking=False` drops the how-to-say-it guidance, for prompts whose output is stored rather
    than said: a summary must keep full dates, the opposite of what Rudi says to a person.
    """
    z = zone(tz)
    local = (now or now_utc()).astimezone(z)
    return ("[Now: it is %s %d %s %d, %s in %s. Every time in these notes is already worked out "
            "against this moment — trust labels like \"yesterday\", \"still to come\" or "
            "\"already passed\" rather than calculating dates yourself.%s]"
            % (_DAYS[local.weekday()], local.day, _MONTHS[local.month - 1], local.year,
               local.strftime("%H:%M"), z.key, (" " + SPEAK_TIMES) if speaking else ""))


def render(history, now: Optional[datetime.datetime] = None, tz: str = "",
           gap: datetime.timedelta = GAP) -> list:
    """History as the model should see it: role and content only, marked wherever time moved.

    The marker goes INSIDE the message text rather than as a system message between turns. Groq
    accepts a mid-history system message, but several chat templates served through vLLM refuse
    any system message that is not the first — and that is exactly the kind of provider the
    cascade fails over to. A marker the model might echo is caught by strip_markers(); a template
    that rejects the request is an outage.

    Entries written before timestamps existed have no `at` and pass through unmarked.
    """
    now = now or now_utc()
    z = zone(tz)
    out, prev = [], None
    for entry in history or []:
        content = entry.get("content", "")
        moment = parse(entry.get("at")) if entry.get("at") else None
        if moment is not None:
            if prev is None:
                moved = now - moment >= gap
            else:
                moved = (moment - prev >= gap
                         or moment.astimezone(z).date() != prev.astimezone(z).date())
            if moved:
                content = "[Time marker: sent %s]\n%s" % (humanize(moment, now, tz), content)
            prev = moment
        out.append({"role": entry.get("role", "user"), "content": content})
    return out


def strip_markers(text: str) -> str:
    """Belt and braces: a marker the model copied into its reply never reaches the person."""
    original = text or ""
    cleaned = MARKER_RE.sub("", original)
    return cleaned.strip() if cleaned != original else original
