"""
MEET_RUDI Training Arena — SKILL BRIEFS (format rudi-skill-brief@1, docs/skill-brief-format.md).

A skill brief is the structured input the generator builds a batch from: a Markdown file with YAML
settings at the top and fixed sections in which every requirement is one bullet with an ID:

    - **B3** (each reply) Asks at most one question per reply.
    - **S2** Doubting a tip
      - users: P6
      - mode: goal
      - checks: B8, N4
      - pass when: Rudi asks what makes them doubt it before offering anything else.

The IDs carry through: the generator writes criteria that cite them ("[B8][N4] …"), the judge's
verdicts quote those criteria, and the Improvement Extractor reads them in the results.

This module only reads and checks a brief and lays out its cases (slots); it never calls Claude.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import yaml  # noqa: E402

import arena_format  # noqa: E402

FORMAT = "rudi-skill-brief@1"
MAX_CHARS = 40000
CATALOGUE = os.path.normpath(os.path.join(HERE, "..", "seed", "skills", "rudi-skills.yaml"))

# key, heading (lower case), ID prefix (None: prose only), required
SECTIONS = (
    ("purpose", "purpose", None, True),
    ("scope", "scope", None, False),
    ("outcome", "outcome", "O", True),
    ("must_do", "must do", "B", True),
    ("must_never", "must never", "N", True),
    ("collide", "when rules collide", "R", False),
    ("boundaries", "boundaries", "L", False),
    ("users", "users", "P", True),
    ("scenarios", "scenarios", "S", True),
    ("examples", "examples", "E", False),
    ("limits", "known limits", "K", False),
)
PREFIX = {p: k for k, _, p, _ in SECTIONS if p}
TITLES = {k: h for k, h, _, _ in SECTIONS}
TAGS = {"B": {"each reply": "reply", "whole conversation": "conversation", "at the end": "end"},
        "N": {"violation": "violation", "defect": "defect"}}
FIELDS = {"P": ("first message", "wants", "resists", "reveals when asked", "won over when", "style"),
          "S": ("users", "mode", "setup", "checks", "pass when", "fail when", "cases"),
          "L": ("examples",), "E": ("user", "good", "bad", "why")}
REQUIRED_FIELDS = {"P": ("wants", "won over when"), "S": ("users", "mode", "checks", "pass when")}
CHECKABLE = set("BNRLOK")
MODES = {"scripted": "scripted", "goal": "intent", "hybrid": "hybrid"}

H2 = re.compile(r"^##\s+(?:\d+[.)]?\s*)?(.+?)\s*(?:\(([A-Z])\))?\s*$")
ITEM = re.compile(r"^[-*]\s+\*\*([A-Z]\d{1,3})\*\*\s*(?:\(([^)]*)\))?\s*(.*)$")
FIELD = re.compile(r"^\s{2,}[-*]\s+([A-Za-z][A-Za-z ]{0,30}?)\s*:\s*(.*)$")
ID_REF = re.compile(r"\b([A-Z]\d{1,3})\b")
BRIEF_ID = re.compile(r"^[a-z][a-z0-9-]{2,40}$")


class BriefError(Exception):
    """The brief cannot be used. `.issues` holds (line, message) pairs, line 0 for the whole file."""

    def __init__(self, issues):
        self.issues = issues
        first = "; ".join(("line %d: %s" % (ln, m)) if ln else m for ln, m in issues[:6])
        more = " (and %d more)" % (len(issues) - 6) if len(issues) > 6 else ""
        super().__init__("The skill brief has %d problem%s — %s%s" % (len(issues), "" if len(issues) == 1 else "s", first, more))


def _section_key(name):
    n = " ".join(name.lower().split())
    for k, h, _, _ in SECTIONS:
        if n == h or n.startswith(h + " ") or n.startswith(h + " —") or n.startswith(h + ":"):
            return k
    return None


def _num(v, lo, hi, what, issues, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        issues.append((0, "%s must be a number." % what))
        return lo
    if not lo <= x <= hi:
        issues.append((0, "%s must be between %s and %s." % (what, lo, hi)))
        return min(hi, max(lo, x))
    return x


def _ids(text):
    return ID_REF.findall(text or "")


def _catalogue_ids():
    try:
        with open(CATALOGUE, encoding="utf-8") as f:
            cat = yaml.safe_load(f) or {}
        return {s.get("id") for s in cat.get("skills") or [] if isinstance(s, dict)}
    except (OSError, yaml.YAMLError):
        return None


def parse(text):
    """Read and check a brief. Returns the brief; raises BriefError with every problem found."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    issues, warnings = [], []
    if len(text) > MAX_CHARS:
        raise BriefError([(0, "The brief is over %d characters." % MAX_CHARS)])
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise BriefError([(1, "A skill brief starts with a line '---', its settings, and another '---'.")])
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise BriefError([(1, "The settings at the top are not closed by a line '---'.")])
    try:
        meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as ex:
        mark = getattr(ex, "problem_mark", None)
        raise BriefError([((mark.line + 2) if mark else 1, "The settings are not valid YAML: %s" % getattr(ex, "problem", ex))])
    if not isinstance(meta, dict):
        raise BriefError([(2, "The settings must be 'name: value' lines.")])

    sections, items, order, seen = {}, {}, [], {}
    key, item, field = None, None, None
    for i in range(end + 1, len(lines)):
        ln, no = lines[i], i + 1
        m = H2.match(ln)
        if m:
            key, item, field = _section_key(m.group(1)), None, None
            if key is None:
                warnings.append((no, "Section “%s” is not part of the format; it is ignored." % m.group(1).strip()))
                key = "_ignored"
            elif key in seen:
                issues.append((no, "Section “%s” appears twice." % TITLES[key]))
            seen[key] = no
            sections.setdefault(key, "")
            continue
        if key is None or key == "_ignored":
            continue
        prefix = next((p for k2, _, p, _ in SECTIONS if k2 == key), None)
        mi = ITEM.match(ln) if prefix else None
        if mi:
            iid, tag, body = mi.group(1), (mi.group(2) or "").strip().lower(), mi.group(3).strip()
            if iid[0] != prefix:
                issues.append((no, "%s is in “%s”, where IDs start with %s." % (iid, TITLES[key], prefix)))
            if iid in items:
                issues.append((no, "%s is used twice (first on line %d)." % (iid, items[iid]["line"])))
            item = {"id": iid, "section": key, "tag": tag, "text": body, "fields": {}, "line": no}
            items[iid], field = item, None
            order.append(iid)
            continue
        mf = FIELD.match(ln) if prefix and item else None
        if mf:
            field = " ".join(mf.group(1).lower().split())
            item["fields"][field] = mf.group(2).strip()
            if field not in FIELDS.get(item["id"][0], ()):
                warnings.append((no, "%s: “%s” is not a field the generator reads." % (item["id"], field)))
            continue
        if not ln.strip():
            field = None
            continue
        if item and ln.startswith((" ", "\t")):
            if field:
                item["fields"][field] = (item["fields"][field] + " " + ln.strip()).strip()
            else:
                item["text"] = (item["text"] + " " + ln.strip()).strip()
            continue
        item, field = None, None
        sections[key] = (sections[key] + "\n" + ln).strip("\n")

    # ---- settings
    if meta.get("format") != FORMAT:
        issues.append((2, "The settings need 'format: %s'." % FORMAT))
    bid = str(meta.get("id") or "")
    if not BRIEF_ID.match(bid):
        issues.append((2, "'id' must be lower-case letters, digits and hyphens, e.g. gym-01-first-contact."))
    if not str(meta.get("title") or "").strip():
        issues.append((2, "'title' is missing."))
    if meta.get("version") in (None, ""):
        issues.append((2, "'version' is missing."))
    if str(meta.get("channel") or "chat") != "chat":
        issues.append((2, "'channel' must be chat: the arena runs chat conversations only."))
    langs = meta.get("languages") or {"en": 100}
    if isinstance(langs, list):
        langs = {x: 100.0 / len(langs) for x in langs} if langs else {}
    if not isinstance(langs, dict) or not langs or any(k not in ("en", "nl") for k in langs):
        issues.append((2, "'languages' must give a share to en and/or nl, e.g. {en: 70, nl: 30}."))
        langs = {"en": 100}
    else:
        try:
            langs = {k: float(v) for k, v in langs.items() if float(v) > 0}
        except (TypeError, ValueError):
            langs = {}
        if not langs or abs(sum(langs.values()) - 100) > 0.5:
            issues.append((2, "The 'languages' shares must add up to 100."))
            langs = langs or {"en": 100}
    b = meta.get("batch") or {}
    if not isinstance(b, dict):
        issues.append((2, "'batch' must hold name, cases, max_turns, … as 'name: value' lines."))
        b = {}
    batch = {"name": " ".join(str(b.get("name") or meta.get("title") or bid).split())[:80],
             "cases": _num(b.get("cases", 10), 1, 100, "batch.cases", issues, int),
             "max_turns": _num(b.get("max_turns", 12), 2, 40, "batch.max_turns", issues, int),
             "max_attempts": _num(b.get("max_attempts", 3), 0, 10, "batch.max_attempts", issues, int),
             "case_budget": _num(b.get("case_budget_eur", 0.05), 0.01, 0.5, "batch.case_budget_eur", issues)}
    batch["batch_budget"] = _num(b.get("batch_budget_eur", max(1.0, round(batch["cases"] * batch["case_budget"], 2))), 0.05, 100,
                                 "batch.batch_budget_eur", issues)
    if batch["case_budget"] > batch["batch_budget"]:
        issues.append((2, "batch.case_budget_eur is above batch.batch_budget_eur."))
    skills = meta.get("catalogue_skills") or []
    if not isinstance(skills, list):
        issues.append((2, "'catalogue_skills' must be a list of skill ids."))
        skills = []
    known = _catalogue_ids()
    for s in skills:
        if known is not None and s not in known:
            issues.append((2, "catalogue_skills: “%s” is not in the skills catalogue (seed/skills/rudi-skills.yaml)." % s))

    # ---- sections and items
    for k, h, p, req in SECTIONS:
        if req and k not in seen:
            issues.append((0, "Section “%s” is missing." % h.capitalize()))
        elif req and p and not any(it["section"] == k for it in items.values()):
            issues.append((seen.get(k, 0), "Section “%s” has no items (bullets starting with **%s1** …)." % (h.capitalize(), p)))
    for it in items.values():
        p = it["id"][0]
        if p in TAGS:
            if it["tag"] and it["tag"] not in TAGS[p]:
                issues.append((it["line"], "%s: “(%s)” — use one of: %s." % (it["id"], it["tag"], ", ".join(TAGS[p]))))
            it["when"] = TAGS[p].get(it["tag"]) or ("conversation" if p == "B" else "defect")
        if not it["text"]:
            issues.append((it["line"], "%s has no text." % it["id"]))
        for f in REQUIRED_FIELDS.get(p, ()):
            if not it["fields"].get(f):
                issues.append((it["line"], "%s needs “%s:”." % (it["id"], f)))
    total_explicit, scenarios = 0, [it for it in items.values() if it["section"] == "scenarios"]
    for s in scenarios:
        f = s["fields"]
        mode = f.get("mode", "").strip().lower()
        if mode and mode not in MODES:
            issues.append((s["line"], "%s: mode must be scripted, goal or hybrid." % s["id"]))
        s["mode"] = MODES.get(mode, "intent")
        s["users"] = [u for u in _ids(f.get("users")) if u.startswith("P")]
        for u in s["users"]:
            if u not in items:
                issues.append((s["line"], "%s names user %s, which is not in Users." % (s["id"], u)))
        if f.get("users") and not s["users"]:
            issues.append((s["line"], "%s: users must be P-ids, e.g. 'users: P2, P5'." % s["id"]))
        s["checks"] = _ids(f.get("checks"))
        for c in s["checks"]:
            if c[0] not in CHECKABLE:
                issues.append((s["line"], "%s checks %s; checks name B, N, R, L, O or K items." % (s["id"], c)))
            elif c not in items:
                issues.append((s["line"], "%s checks %s, which is not in the brief." % (s["id"], c)))
        s["cases"] = None
        if f.get("cases"):
            try:
                s["cases"] = int(f["cases"])
                if s["cases"] < 1:
                    raise ValueError
                total_explicit += s["cases"]
            except ValueError:
                issues.append((s["line"], "%s: cases must be a whole number from 1." % s["id"]))
                s["cases"] = None
    if scenarios:
        free = [s for s in scenarios if not s["cases"]]
        if total_explicit + len(free) > batch["cases"]:
            issues.append((2, "batch.cases is %d, but the scenarios need at least %d (one each, plus the cases they set)."
                           % (batch["cases"], total_explicit + len(free))))
        elif not free and total_explicit != batch["cases"]:
            issues.append((2, "batch.cases is %d, but the scenarios' cases add up to %d." % (batch["cases"], total_explicit)))

    # ---- synthetic only
    _, found = arena_format.mask_identifiers(text)
    if found:
        issues.append((0, "The brief contains what looks like %s. Remove it: briefs are synthetic." % ", ".join(sorted(found))))

    if issues:
        raise BriefError(sorted(issues, key=lambda x: x[0]))
    return {"format": FORMAT, "id": bid, "title": str(meta["title"]).strip(), "version": str(meta["version"]),
            "status": str(meta.get("status") or "draft"), "release_gate": str(meta.get("release_gate") or ""),
            "catalogue_skills": skills, "languages": langs, "batch": batch, "sections": sections,
            "items": items, "order": order, "warnings": warnings, "text": text}


# --------------------------------------------------------------------------- cases
def _counts(brief):
    sc = [brief["items"][i] for i in brief["order"] if i.startswith("S")]
    free = [s for s in sc if not s["cases"]]
    rest = brief["batch"]["cases"] - sum(s["cases"] or 0 for s in sc)
    out, k = {}, 0
    for s in sc:
        if s["cases"]:
            out[s["id"]] = s["cases"]
        else:
            out[s["id"]] = rest // len(free) + (1 if k < rest % len(free) else 0)
            k += 1
    return out


def _languages(langs, n):
    """A language per slot, spread evenly in the brief's shares (largest deficit first)."""
    got, out = {k: 0 for k in langs}, []
    for i in range(n):
        lang = max(langs, key=lambda k: (langs[k] * (i + 1) / 100.0 - got[k], langs[k]))
        got[lang] += 1
        out.append(lang)
    return out


def persona_id(user, lang):
    return "%s-%s" % (user.lower(), lang)


def slots(brief):
    """One slot per case: scenario, user type, mode, language and the persona id it will be cast as.
    Scenarios in the brief's order; within a scenario its users take turns."""
    counts = _counts(brief)
    langs = _languages(brief["languages"], sum(counts.values()))
    out, n = [], 0
    for sid in [i for i in brief["order"] if i.startswith("S")]:
        s = brief["items"][sid]
        for k in range(counts[sid]):
            users = s["users"] or [p for p in brief["order"] if p.startswith("P")][:1]
            user = users[k % len(users)]
            out.append({"slot": n + 1, "mode": s["mode"], "lang": langs[n], "scenario": sid, "user": user,
                        "persona": persona_id(user, langs[n])})
            n += 1
    return out


# --------------------------------------------------------------------------- text for prompts and the console
def item_text(it):
    """One item as the generator shows it to Claude: ID, tag, text and fields."""
    head = "%s%s %s" % (it["id"], " (%s)" % it["tag"] if it["tag"] else "", it["text"])
    return head + "".join("\n    %s: %s" % (k, v) for k, v in it["fields"].items())


def summary(brief):
    """What the console shows before planning."""
    counts = _counts(brief)
    it = brief["items"]
    by = {}
    for i in brief["order"]:
        by[i[0]] = by.get(i[0], 0) + 1
    return {"id": brief["id"], "title": brief["title"], "version": brief["version"], "status": brief["status"],
            "release_gate": brief["release_gate"], "languages": brief["languages"], "batch": brief["batch"],
            "catalogue_skills": brief["catalogue_skills"], "counts": by,
            "scenarios": [{"id": s, "title": it[s]["text"], "mode": {"intent": "goal"}.get(it[s]["mode"], it[s]["mode"]),
                           "users": it[s]["users"], "cases": counts[s], "checks": it[s]["checks"]}
                          for s in brief["order"] if s.startswith("S")],
            "users": [{"id": p, "title": it[p]["text"]} for p in brief["order"] if p.startswith("P")],
            "warnings": ["line %d: %s" % w for w in brief["warnings"]]}
