"""
MEET_RUDI Training Arena — the IMPROVEMENT EXTRACTOR's core: from a results log to three lists —
the application errors met during the run, Rudi's weak points, and feature requests against the
skills catalogue — and a standalone HTML report.

Deterministic first: the results file is parsed and every error is collected exactly as the runner
recorded it; the cases are boiled down to a DIGEST (cases that did not pass first, with their
checks, the judge's reasons, errors and turns). Only that digest and the skills catalogue go to the
model, which answers in JSON. Skill ids it names are checked against the catalogue; recorded
errors it leaves out are added back. The model is injected — `ask(prompt) -> text`: the arena's
own judge cascade in AWS (extractor.py), or Claude Code on the user's computer (generator.py).

Results logs hold synthetic conversations only (arena inputs are validated as synthetic).
"""

import html
import json
import os
import re

import yaml

import arena_common
import arena_format

RESULTS_FORMAT = "rudi-arena/results@1"
REPORT_FORMAT = "rudi-arena/improvements@1"
DIGEST_CHARS = 110000                 # ≈ 35K tokens: fits the arena's OVH model and Claude alike
MSG_CHARS = 700
NOT_OK = ("violation", "failed", "error", "over_budget")
ORDER = {"violation": 0, "failed": 1, "error": 2, "over_budget": 3, "completed": 4, "success": 5, "cancelled": 6}
SEVERITY = ("high", "medium", "low")
PRIORITY = ("P1", "P2", "P3")
HERE = os.path.dirname(os.path.abspath(__file__))
CATALOGUE_SEED = os.path.normpath(os.path.join(HERE, "..", "seed", "skills", "rudi-skills.yaml"))


class ImproveError(Exception):
    """Shown to the user as it is."""


# --------------------------------------------------------------------------- reading
def read_results(text):
    """{header, cases, parts} from one results file, or several parts pasted together."""
    try:
        docs = [d for d in yaml.safe_load_all(text or "") if isinstance(d, dict)]
    except yaml.YAMLError as e:
        raise ImproveError("This is not a readable results file (YAML error near line %s)." % (getattr(getattr(e, "problem_mark", None), "line", -1) + 1))
    headers = [d for d in docs if d.get("format") == RESULTS_FORMAT]
    cases = [d for d in docs if isinstance(d.get("case"), dict) and isinstance(d.get("result"), dict)]
    if not cases:
        if any(d.get("format") == "rudi-arena/input@1" for d in docs):
            raise ImproveError("This is a batch INPUT file. Choose a results file (…__results__p01ofNN.yaml).")
        raise ImproveError("No case results in this file. Choose a Training Arena results file (…__results__p01ofNN.yaml).")
    seen, unique = set(), []
    for c in cases:
        cid = c["case"].get("id") or c["case"].get("key")
        if cid not in seen:
            seen.add(cid)
            unique.append(c)
    h = headers[0] if headers else {}
    return {"header": h, "cases": unique, "parts": len(headers) or 1}


def _txt(v, limit):
    s = " ".join(str(v if v is not None else "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def app_errors(results):
    """Every error the runner recorded, grouped by code and the start of its message."""
    groups = {}
    for c in results["cases"]:
        cid = c["case"].get("id") or c["case"].get("key")
        res = c["result"]
        for e in res.get("errors") or []:
            code = str(e.get("code") or "error")
            detail = _txt(e.get("detail"), 300)
            sig = (code, re.sub(r"[0-9a-f]{8,}|\d+(\.\d+)?", "#", detail)[:90])
            g = groups.setdefault(sig, {"code": code, "detail": detail, "count": 0, "recovered": 0, "cases": [], "turns": []})
            g["count"] += 1
            g["recovered"] += 1 if e.get("recovered") else 0
            if cid not in g["cases"]:
                g["cases"].append(cid)
            g["turns"].append(e.get("at_turn"))
        stop = res.get("stop_reason")
        if stop in ("engine_error", "tester_error", "judge_error", "timeout", "runner_lost") and not res.get("errors"):
            g = groups.setdefault((stop, ""), {"code": stop, "detail": "case stopped by %s" % stop, "count": 0, "recovered": 0, "cases": [], "turns": []})
            g["count"] += 1
            g["cases"].append(cid)
    return sorted(groups.values(), key=lambda g: (-(g["count"] - g["recovered"]), -g["count"]))


def overview(results):
    out = []
    for c in results["cases"]:
        cs, r = c["case"], c["result"]
        out.append({"id": cs.get("id"), "key": cs.get("key"), "title": cs.get("title"), "mode": cs.get("mode"),
                    "locale": (cs.get("locale") or {}).get("requested") if isinstance(cs.get("locale"), dict) else cs.get("locale"),
                    "outcome": r.get("outcome"), "stop": r.get("stop_reason"), "verdict": _txt(r.get("verdict"), 220)})
    return out


def _case_block(c, with_transcript=True):
    cs, r = c["case"], c["result"]
    loc = cs.get("locale")
    loc = loc.get("requested") if isinstance(loc, dict) else loc
    persona = (cs.get("persona") or {}).get("display_name") if isinstance(cs.get("persona"), dict) else cs.get("persona")
    L = ["### CASE %s · %s — %s" % (cs.get("id"), cs.get("key"), _txt(cs.get("title"), 160)),
         "mode %s · language %s · persona %s · tags %s" % (cs.get("mode"), loc, persona, ", ".join(cs.get("tags") or [])),
         "OUTCOME %s (stopped by %s): %s" % (r.get("outcome"), r.get("stop_reason"), _txt(r.get("verdict"), 400))]
    for ch in r.get("checks") or []:
        L.append("- check [%s] %s: %s — judge: %s" % (ch.get("kind"), ch.get("result"), _txt(ch.get("check"), 400), _txt(ch.get("why"), 300)))
    for e in r.get("errors") or []:
        L.append("- error at turn %s: %s %s%s" % (e.get("at_turn"), e.get("code"), _txt(e.get("detail"), 240), " (recovered)" if e.get("recovered") else ""))
    if with_transcript:
        for t in c.get("transcript") or []:
            m = t.get("meta") or {}
            who = "RUDI" if t.get("from") == "rudi" else "USER"
            tag = []
            if who == "RUDI":
                tag = [x for x in (m.get("model"), m.get("phase"), ("%s ms" % m["latency_ms"]) if m.get("latency_ms") else None) if x]
                if (m.get("judge") or {}).get("why"):
                    tag.append("judge: " + _txt(m["judge"]["why"], 160))
            L.append("%s %s%s: %s" % (who, t.get("turn"), " [" + "; ".join(str(x) for x in tag) + "]" if tag else "", _txt(t.get("text"), MSG_CHARS)))
    return "\n".join(L)


def digest(results, limit=DIGEST_CHARS):
    """The text the model reads: header, then cases — those that did not pass first, in full;
    the rest in full while there is room, as one line each after that."""
    h = results["header"]
    b, eng, s = h.get("batch") or {}, h.get("engine") or {}, h.get("summary") or {}
    head = ["BATCH %s — %s" % (b.get("id"), b.get("name")), "Description: %s" % _txt(b.get("description"), 600),
            "Tags: %s · Rudi release %s · channel %s" % (", ".join(b.get("tags") or []), eng.get("rudi_release"), eng.get("channel")),
            "Outcomes: %s" % json.dumps((s.get("cases") or {}), ensure_ascii=False)]
    cases = sorted(results["cases"], key=lambda c: (ORDER.get(c["result"].get("outcome"), 9), str(c["case"].get("id"))))
    out, used, short = ["\n".join(head)], len(head[0]) + 400, []
    full = 0
    for c in cases:
        block = _case_block(c)
        if used + len(block) <= limit:
            out.append(block)
            used += len(block)
            full += 1
        else:
            short.append(_case_block(c, with_transcript=False).split("\n")[0] + " — " + _txt(c["result"].get("verdict"), 160))
    if short:
        out.append("### %d more cases, summary only (no room for their transcripts):\n%s" % (len(short), "\n".join(short)))
    return "\n\n".join(out), {"cases": len(cases), "in_full": full, "summarised": len(short), "chars": used}


# --------------------------------------------------------------------------- the catalogue
def load_catalogue(text=None, path=CATALOGUE_SEED):
    if text is None:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            raise ImproveError("The skills catalogue is missing (%s)." % os.path.basename(path))
    cat = yaml.safe_load(text) or {}
    skills = [s for s in (cat.get("skills") or []) if isinstance(s, dict) and s.get("id")]
    if not skills:
        raise ImproveError("The skills catalogue is empty.")
    return {"version": cat.get("version"), "skills": skills}


def catalogue_text(cat):
    L = []
    for s in cat["skills"]:
        L.append("- %s — %s%s (%s; channels: %s; %s): %s Expected: %s Code: %s%s" % (
            s["id"], s.get("name"), " [PLANNED, not built yet]" if s.get("status") == "planned" else "", s.get("domain"),
            ", ".join(s.get("channels") or []),
            "the arena's chat exercises it" if s.get("arena") == "chat" else "not exercised by the arena",
            _txt(s.get("summary"), 400), _txt(s.get("expected"), 300), "; ".join(s.get("code") or [])[:400],
            " Known gaps: " + _txt(s.get("gaps"), 300) if s.get("gaps") else ""))
    return "\n".join(L)


# --------------------------------------------------------------------------- the model
PROMPT = """You are reviewing the results of a Training Arena batch for Rudi, an AI health-accountability coach for adults with diabetes (WhatsApp, web chat and voice; English and Flemish Dutch). Rudi must never give medical advice. The arena plays simulated users against the deployed Rudi and a judge scores his replies. All conversations are synthetic.

Your job: turn this run into improvement work.

THE SKILLS CATALOGUE — Rudi's engine split into skills, with where each lives in the code:
{catalogue}

APPLICATION ERRORS the runner recorded (grouped; authoritative):
{errors}

THE RUN (cases that did not pass come first):
{digest}

Answer with ONLY one JSON object (no prose, no code fence), with these keys:
"summary": 2-3 sentences on what this run shows.
"app_errors": every application error worth fixing — not Rudi's behaviour, but failures of the platform, the engine, the providers or the arena (timeouts, rate limits, rejected JSON, crashes, missing keys). For each: "title", "severity" (high|medium|low), "count", "cases" (case ids), "where" (component or code location if evident, else ""), "detail" (what happened), "probable_cause". Cover every group listed above; merge groups with one cause.
"deficiencies": Rudi's weak points shown by this run. For each: "title", "skill" (one id from the catalogue), "severity" (high|medium|low), "cases" (case ids), "evidence" (1-3 items: {{"case": id, "turn": number, "quote": "a short quote of Rudi's reply"}}), "description" (what goes wrong and why it matters). Only what the transcripts and judge reasons show — no speculation. Rank by severity.
"feature_requests": changes that would close those gaps, written as direct, self-contained tasks for a coding agent who knows this code base. For each: "title" (imperative), "skill" (catalogue id), "priority" (P1|P2|P3), "problem" (1-2 sentences, with the case ids), "change" (exactly what to change, where — prompt section, function, config — and how), "acceptance_criteria" (2-5 testable statements), "code_pointers" (files / functions / prompt sections from the catalogue), "addresses" (titles of the deficiencies it fixes), "test_case" (a Training Arena case that would prove the fix, in one or two sentences). Prefer small, targeted changes; never propose giving medical advice.
Use only skill ids from the catalogue. If nothing went wrong in a list, return it empty."""


def build_prompt(results, cat, errs=None, limit=DIGEST_CHARS):
    errs = app_errors(results) if errs is None else errs
    etext = "\n".join("- %s × %d (%d recovered) in cases %s: %s" % (g["code"], g["count"], g["recovered"], ", ".join(g["cases"][:12]), g["detail"])
                      for g in errs) or "- none"
    text, stats = digest(results, limit)
    return PROMPT.format(catalogue=catalogue_text(cat), errors=etext, digest=text), stats


def parse_answer(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    for cand in (t, t[t.find("{"):t.rfind("}") + 1] if "{" in t else ""):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    raise ImproveError("The model's answer was not the JSON it was asked for.")


def _list(v):
    return v if isinstance(v, list) else []


def _strs(v, limit=400, n=8):
    return [_txt(x, limit) for x in _list(v) if str(x or "").strip()][:n]


def normalize(answer, cat, errs, case_ids):
    """The model's answer, cut to the report's shape. Unknown skill ids are kept but flagged;
    recorded error groups the model left out are added back."""
    ids = {s["id"] for s in cat["skills"]}
    known = set(case_ids)
    notes = []

    def skill(v):
        s = str(v or "").strip()
        if s in ids:
            return s
        if s:
            notes.append("“%s” is not in the skills catalogue" % s)
        return "unmapped" + (":" + s if s else "")

    def cases(v):
        return [str(x) for x in _list(v) if str(x) in known][:30]

    out = {"summary": _txt(answer.get("summary"), 900), "app_errors": [], "deficiencies": [], "feature_requests": []}
    for e in _list(answer.get("app_errors")):
        if isinstance(e, dict) and e.get("title"):
            out["app_errors"].append({"title": _txt(e["title"], 160), "severity": e.get("severity") if e.get("severity") in SEVERITY else "medium",
                                      "count": int(e.get("count") or 0) if str(e.get("count") or "0").isdigit() else 0,
                                      "cases": cases(e.get("cases")), "where": _txt(e.get("where"), 240),
                                      "detail": _txt(e.get("detail"), 600), "probable_cause": _txt(e.get("probable_cause"), 500)})
    covered = " ".join((e["title"] + " " + e["detail"]).lower() for e in out["app_errors"])
    for g in errs:
        if g["code"].lower() not in covered:
            out["app_errors"].append({"title": "%s (as recorded)" % g["code"], "severity": "medium" if g["count"] > g["recovered"] else "low",
                                      "count": g["count"], "cases": g["cases"][:30], "where": "", "detail": g["detail"],
                                      "probable_cause": "", "recorded_only": True})
    for d in _list(answer.get("deficiencies")):
        if isinstance(d, dict) and d.get("title"):
            ev = [{"case": str(x.get("case")), "turn": x.get("turn"), "quote": _txt(x.get("quote"), 300)}
                  for x in _list(d.get("evidence")) if isinstance(x, dict) and str(x.get("case")) in known][:3]
            out["deficiencies"].append({"title": _txt(d["title"], 160), "skill": skill(d.get("skill")),
                                        "severity": d.get("severity") if d.get("severity") in SEVERITY else "medium",
                                        "cases": cases(d.get("cases")), "evidence": ev, "description": _txt(d.get("description"), 900)})
    out["deficiencies"].sort(key=lambda d: SEVERITY.index(d["severity"]))
    for i, f in enumerate([f for f in _list(answer.get("feature_requests")) if isinstance(f, dict) and f.get("title")], start=1):
        out["feature_requests"].append({"id": "FR-%d" % i, "title": _txt(f["title"], 180), "skill": skill(f.get("skill")),
                                        "priority": f.get("priority") if f.get("priority") in PRIORITY else "P2",
                                        "problem": _txt(f.get("problem"), 700), "change": _txt(f.get("change"), 1600),
                                        "acceptance_criteria": _strs(f.get("acceptance_criteria"), 400, 6),
                                        "code_pointers": _strs(f.get("code_pointers"), 200, 8),
                                        "addresses": _strs(f.get("addresses"), 160, 6), "test_case": _txt(f.get("test_case"), 600)})
    out["feature_requests"].sort(key=lambda f: PRIORITY.index(f["priority"]))
    for i, f in enumerate(out["feature_requests"], start=1):
        f["id"] = "FR-%d" % i
    out["notes"] = sorted(set(notes))
    return out


# --------------------------------------------------------------------------- the report
def report_name(source_name, analysis_id):
    """<results file stem>__improvements__<analysis id>.html — the source stays recognisable."""
    base = os.path.basename(str(source_name or "results.yaml"))
    stem = re.sub(r"\.(ya?ml|json|jsonl)$", "", base, flags=re.I)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-")[:120] or "results"
    return "%s__improvements__%s.html" % (stem, analysis_id)


def markdown(rep, meta):
    """The feature requests as a hand-over for a coding agent."""
    L = ["# Feature requests from Training Arena run %s" % (meta.get("batch_id") or meta.get("source")),
         "", "Source: `%s` · analysed %s with %s · skills catalogue %s" % (meta.get("source"), meta.get("generated"), meta.get("engine"), meta.get("catalogue")),
         "", rep.get("summary") or "", ""]
    for f in rep["feature_requests"]:
        L += ["## %s · %s — %s" % (f["id"], f["priority"], f["title"]), "", "**Skill:** `%s`" % f["skill"], "",
              "**Problem.** %s" % f["problem"], "", "**Change.** %s" % f["change"], ""]
        if f["acceptance_criteria"]:
            L += ["**Acceptance criteria**"] + ["- %s" % a for a in f["acceptance_criteria"]] + [""]
        if f["code_pointers"]:
            L += ["**Code pointers:** " + ", ".join("`%s`" % c for c in f["code_pointers"]), ""]
        if f["test_case"]:
            L += ["**Arena test case.** %s" % f["test_case"], ""]
    return "\n".join(L).strip() + "\n"


CSS = """:root{--bg:#f5f6fa;--card:#fff;--ink:#253e7f;--ink2:#3a4256;--muted:#6b7280;--line:#e3e5ec;--accent:#eb23c5;--soft:#eef2fb;
--hi:#b23b3b;--hi-s:#fdeaea;--md:#a06004;--md-s:#fdf1e0;--lo:#3979aa;--lo-s:#e5eef7;--ok:#16a34a;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#141826;--card:#1d2335;--ink:#dfe6ff;--ink2:#c3cadb;--muted:#9aa3b5;--line:#2c3449;--soft:#232b42;
--hi:#ff8a8a;--hi-s:#3a2026;--md:#f3b65a;--md-s:#382c18;--lo:#8ab8e6;--lo-s:#1e2d40;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink2);font:15px/1.55 "Nunito",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:64rem;margin:0 auto;padding:2rem 1.2rem 4rem}h1,h2,h3{color:var(--ink);line-height:1.25;margin:0}h1{font-size:1.6rem}h2{font-size:1.15rem}h3{font-size:1rem}
.meta{color:var(--muted);font-size:.85rem;margin:.4rem 0 1rem}.meta code{font-size:.8rem}.sum{background:var(--card);border-radius:.7rem;padding:1rem 1.2rem;margin:1rem 0}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.8rem;margin:1rem 0}.tile{background:var(--card);border-radius:.7rem;padding:.8rem 1rem}
.tile b{display:block;font-size:1.6rem;color:var(--ink)}.tile span{font-size:.8rem;color:var(--muted)}
section{margin-top:2rem}section>h2{display:flex;gap:.6rem;align-items:center;padding-bottom:.4rem;border-bottom:2px solid var(--line)}
.n{font-size:.75rem;background:var(--soft);color:var(--ink);border-radius:99px;padding:.1rem .55rem}
.card{background:var(--card);border-radius:.7rem;padding:.9rem 1.1rem;margin-top:.8rem;border-left:4px solid var(--line)}
.card.high{border-left-color:var(--hi)}.card.medium{border-left-color:var(--md)}.card.low{border-left-color:var(--lo)}
.chips{display:flex;flex-wrap:wrap;gap:.35rem;margin:.35rem 0}.chip{font-size:.7rem;font-weight:700;border-radius:99px;padding:.1rem .55rem;background:var(--soft);color:var(--ink)}
.chip.high,.chip.P1{background:var(--hi-s);color:var(--hi)}.chip.medium,.chip.P2{background:var(--md-s);color:var(--md)}.chip.low,.chip.P3{background:var(--lo-s);color:var(--lo)}
.chip.skill{font-family:ui-monospace,Consolas,monospace}blockquote{margin:.4rem 0;padding:.3rem .8rem;border-left:3px solid var(--line);color:var(--ink2);font-size:.88rem}
dl{display:grid;grid-template-columns:9rem minmax(0,1fr);gap:.3rem .8rem;margin:.5rem 0 0;font-size:.9rem}dt{color:var(--muted);font-weight:700}dd{margin:0}
ul{margin:.2rem 0 .2rem 1.1rem;padding:0}code{font-family:ui-monospace,Consolas,monospace;font-size:.82em;background:var(--soft);border-radius:.25rem;padding:0 .25rem}
table{width:100%;border-collapse:collapse;font-size:.82rem;background:var(--card);border-radius:.6rem;overflow:hidden}th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}.tw{overflow-x:auto}.empty{color:var(--muted);font-style:italic;margin-top:.6rem}
details{margin-top:.8rem}summary{cursor:pointer;color:var(--ink);font-weight:700}pre{white-space:pre-wrap;background:var(--card);border-radius:.6rem;padding:.8rem;font-size:.8rem}
@media (max-width:40rem){.grid{grid-template-columns:1fr}dl{grid-template-columns:1fr}}"""


def render_html(rep, meta, cases):
    e = html.escape

    def chips(*items):
        return '<div class="chips">' + "".join('<span class="chip %s">%s</span>' % (e(c), e(t)) for c, t in items if t) + "</div>"

    def cl(ids):
        return ", ".join("<code>%s</code>" % e(i) for i in ids) or "—"

    errs = "".join('<div class="card %s"><h3>%s</h3>%s<dl><dt>Cases</dt><dd>%s</dd><dt>What happened</dt><dd>%s</dd>%s%s</dl></div>' % (
        e(x["severity"]), e(x["title"]), chips((x["severity"], x["severity"]), ("", "%d×" % x["count"] if x.get("count") else ""),
                                              ("", "as recorded" if x.get("recorded_only") else "")),
        cl(x["cases"]), e(x["detail"]), ("<dt>Where</dt><dd>%s</dd>" % e(x["where"])) if x.get("where") else "",
        ("<dt>Probable cause</dt><dd>%s</dd>" % e(x["probable_cause"])) if x.get("probable_cause") else "") for x in rep["app_errors"])
    defs = "".join('<div class="card %s"><h3>%s</h3>%s<p>%s</p>%s<dl><dt>Cases</dt><dd>%s</dd></dl></div>' % (
        e(d["severity"]), e(d["title"]), chips((d["severity"], d["severity"]), ("skill", d["skill"])), e(d["description"]),
        "".join("<blockquote>%s, turn %s: “%s”</blockquote>" % (e(v["case"]), e(str(v["turn"])), e(v["quote"])) for v in d["evidence"]),
        cl(d["cases"])) for d in rep["deficiencies"])
    frs = "".join('<div class="card %s" id="%s"><h3>%s · %s</h3>%s<dl><dt>Problem</dt><dd>%s</dd><dt>Change</dt><dd>%s</dd>'
                  '<dt>Acceptance</dt><dd>%s</dd><dt>Code pointers</dt><dd>%s</dd>%s%s</dl></div>' % (
                      {"P1": "high", "P2": "medium", "P3": "low"}[f["priority"]], e(f["id"]), e(f["id"]), e(f["title"]),
                      chips((f["priority"], f["priority"]), ("skill", f["skill"])), e(f["problem"]), e(f["change"]),
                      "<ul>%s</ul>" % "".join("<li>%s</li>" % e(a) for a in f["acceptance_criteria"]) if f["acceptance_criteria"] else "—",
                      ", ".join("<code>%s</code>" % e(c) for c in f["code_pointers"]) or "—",
                      ("<dt>Addresses</dt><dd>%s</dd>" % e("; ".join(f["addresses"]))) if f["addresses"] else "",
                      ("<dt>Arena test</dt><dd>%s</dd>" % e(f["test_case"])) if f["test_case"] else "") for f in rep["feature_requests"])
    rows = "".join("<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
        e(str(c["id"])), e(str(c["key"])), e(str(c["mode"])), e(str(c["outcome"])), e(str(c["stop"])), e(c["verdict"] or "")) for c in cases)
    data = json.dumps({"format": REPORT_FORMAT, "meta": meta, "report": rep}, ensure_ascii=False).replace("</", "<\\/")
    empty = '<p class="empty">Nothing found in this run.</p>'
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s — improvements</title><style>%s</style></head><body><main>"
            "<h1>Improvement report — %s</h1><p class=\"meta\">Source <code>%s</code>%s · %d cases · analysed %s with %s · skills catalogue %s · report <code>%s</code></p>"
            "<div class=\"sum\">%s</div>"
            "<div class=\"grid\"><div class=\"tile\"><b>%d</b><span>application errors</span></div><div class=\"tile\"><b>%d</b><span>weak points of Rudi</span></div>"
            "<div class=\"tile\"><b>%d</b><span>feature requests</span></div></div>"
            "<section><h2>Application errors <span class=\"n\">%d</span></h2>%s</section>"
            "<section><h2>Rudi's weak points <span class=\"n\">%d</span></h2>%s</section>"
            "<section><h2>Feature requests for the coding agent <span class=\"n\">%d</span></h2>%s"
            "<details><summary>All feature requests as Markdown</summary><pre>%s</pre></details></section>"
            "<section><h2>Cases in this run <span class=\"n\">%d</span></h2><div class=\"tw\"><table><thead><tr><th>Case</th><th>Key</th><th>Mode</th><th>Outcome</th><th>Stopped by</th><th>Verdict</th></tr></thead><tbody>%s</tbody></table></div></section>"
            "%s<script type=\"application/json\" id=\"improvements-data\">%s</script></main></body></html>") % (
        e(meta.get("batch_id") or meta.get("source") or "run"), CSS, e(meta.get("batch_name") or meta.get("batch_id") or meta.get("source") or ""),
        e(meta.get("source") or ""), (" · batch <code>%s</code>" % e(meta["batch_id"])) if meta.get("batch_id") else "", len(cases),
        e(meta.get("generated") or ""), e(meta.get("engine") or ""), e(str(meta.get("catalogue") or "")), e(meta.get("report") or ""),
        e(rep.get("summary") or "—"), len(rep["app_errors"]), len(rep["deficiencies"]), len(rep["feature_requests"]),
        len(rep["app_errors"]), errs or empty, len(rep["deficiencies"]), defs or empty, len(rep["feature_requests"]), frs or empty,
        e(markdown(rep, meta)), len(cases), rows,
        ("<p class=\"meta\">Notes: %s</p>" % e("; ".join(rep.get("notes") or []))) if rep.get("notes") else "", data)


# --------------------------------------------------------------------------- the pipeline
def _noop(*a, **k):
    pass


def _n(k, word):
    return "%d %s%s" % (k, word, "" if k == 1 else "s")


def analyze(text, source, ask, engine_label, analysis_id, catalogue=None, progress=_noop, now=None, limit=DIGEST_CHARS,
            mask_ids=False):
    """The whole extraction. `source`: {name, batch_id?, key?}. Returns report, html, markdown,
    file name and stats. `mask_ids`: mask identifier-like strings (e-mail, phone, IBAN) in the
    prompt — for a model without a DPA (project decision 21)."""
    results = read_results(text)
    cat = catalogue or load_catalogue()
    errs = app_errors(results)
    ov = overview(results)
    progress("Read the results: %d cases%s" % (len(ov), ", %d parts" % results["parts"] if results["parts"] > 1 else ""), "done")
    progress("Collected %d kind%s of application error the runner recorded" % (len(errs), "" if len(errs) == 1 else "s"), "done")
    prompt, stats = build_prompt(results, cat, errs, limit)
    masked = {}
    if mask_ids:
        prompt, masked = arena_format.mask_identifiers(prompt)
        if masked:
            progress("Masked %d identifier-like strings before sending (%s)" % (sum(masked.values()), ", ".join(sorted(masked))), "done")
    label = "%s reads the run and drafts the three lists" % engine_label
    progress(label, "run")
    answer = ask(prompt)
    rep = normalize(parse_answer(answer), cat, errs, [c["id"] for c in ov])
    if masked:
        rep["notes"].append("%d identifier-like strings (%s) were masked before the model read the run" % (sum(masked.values()), ", ".join(sorted(masked))))
    progress(label, "done")
    b = results["header"].get("batch") or {}
    when = arena_common.display(now or arena_common.now_utc())                 # the team's (Brussels) clock
    name = report_name(source.get("name"), analysis_id)
    meta = {"source": source.get("name"), "source_key": source.get("key"), "batch_id": source.get("batch_id") or b.get("id"),
            "batch_name": b.get("name"), "generated": when, "engine": engine_label, "catalogue": cat.get("version"),
            "report": name, "analysis_id": analysis_id, "digest": stats, "masked": masked}
    progress("Report ready: %s, %s, %s" % (_n(len(rep["app_errors"]), "error"), _n(len(rep["deficiencies"]), "weak point"),
                                          _n(len(rep["feature_requests"]), "feature request")), "done")
    return {"report": rep, "meta": meta, "cases": ov, "html": render_html(rep, meta, ov), "markdown": markdown(rep, meta), "file": name}
