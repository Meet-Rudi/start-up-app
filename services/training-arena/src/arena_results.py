"""
MEET_RUDI Training Arena — the batch RESULTS format (rudi-arena/results@1): render and plan.

Spec: docs/training-arena-formats.md §4. The runner renders each finished case's document once
(`render_case`) and stores it; the assembler plans the parts from the recorded sizes alone
(`plan_parts`) — so the number of parts is final before anything is written — and then streams
header + case documents into each part.

The YAML is written by hand rather than by a YAML library, so the layout is the documented one:
block style for structure, compact flow mappings for metadata, double-quoted text where a plain
scalar could be misread. Every output is valid YAML (tests load it back).
"""

import re
import io
import csv
import json

from arena_common import est_tokens

FORMAT_OUT = "rudi-arena/results@1"
_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_./@+\-]*( [A-Za-z0-9_./@+\-→()]+)*$")
_RESERVED = {"y", "n", "yes", "no", "true", "false", "on", "off", "null", "~"}


# --------------------------------------------------------------------------- scalars
def scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return "null"
        s = ("%.6f" % v).rstrip("0").rstrip(".")
        return s if s not in ("", "-0") else "0"
    s = str(v)
    if _PLAIN.match(s) and s.lower() not in _RESERVED and len(s) <= 200:
        return s
    return json.dumps(s, ensure_ascii=False)


def flow(v):
    if isinstance(v, dict):
        return "{" + ", ".join("%s: %s" % (scalar(str(k)), flow(x)) for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(flow(x) for x in v) + "]"
    return scalar(v)


def _kv(lines, indent, key, value):
    pad = " " * indent
    if isinstance(value, (dict, list, tuple)):
        lines.append("%s%s: %s" % (pad, key, flow(value)))
    else:
        lines.append("%s%s: %s" % (pad, key, scalar(value)))


def _list_of_flows(lines, indent, key, items):
    pad = " " * indent
    if not items:
        lines.append("%s%s: []" % (pad, key))
        return
    lines.append("%s%s:" % (pad, key))
    for it in items:
        lines.append("%s  - %s" % (pad, flow(it)))


# --------------------------------------------------------------------------- case document
def render_case(rec):
    """One case document (starting with its '---' separator) from a case record (result.json)."""
    c, r = rec["case"], rec["result"]
    L = ["---", "case:"]
    for k in ("id", "key", "title", "mode", "channel", "locale", "persona", "rudi", "tags"):
        if k in c:
            _kv(L, 2, k, c[k])
    L.append("result:")
    for k in ("outcome", "stop_reason", "verdict"):
        _kv(L, 2, k, r.get(k))
    _list_of_flows(L, 2, "checks", r.get("checks") or [])
    for k in ("counts", "cost", "timing", "final_state"):
        _kv(L, 2, k, r.get(k) or {})
    _list_of_flows(L, 2, "errors", r.get("errors") or [])
    tx = rec.get("transcript") or []
    if not tx:
        L.append("transcript: []")
    else:
        L.append("transcript:")
        for e in tx:
            L.append("  - turn: %s" % scalar(e.get("turn")))
            for k in ("from", "at", "sim_at", "text"):
                if e.get(k) is not None:
                    _kv(L, 4, k, e[k])
            _kv(L, 4, "meta", e.get("meta") or {})
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- header document
def render_header(manifest, part, parts, summary, part_rows, partial_at=None):
    """The part header: part / parts / index_file / batch / engine / summary."""
    stem = manifest["stem"]
    L = []
    if partial_at:
        L.append("# Snapshot taken %s while the batch was still running — not the final results." % partial_at)
    L.append("format: %s" % FORMAT_OUT)
    L.append("")
    L.append("part:")
    _kv(L, 2, "index", part["index"])
    _kv(L, 2, "of", len(parts))
    _kv(L, 2, "cases", {"first": part["first_id"], "last": part["last_id"], "count": len(part["seqs"])})
    _kv(L, 2, "est_tokens", part.get("est_tokens", 0))
    _list_of_flows(L, 2, "outcomes", [{"case": x["id"], "key": x.get("key"), "outcome": x.get("outcome"),
                                       "stop": x.get("stop"), "cost_eur": round(float(x.get("cost") or 0), 5)}
                                      for x in part_rows])
    L.append("")
    _list_of_flows(L, 0, "parts", [dict({"part": p["index"], "file": p["file"], "first": p["first"], "last": p["last"],
                                         "cases": len(p["seqs"]), "outcomes": p.get("outcomes") or {}},
                                        **({"oversize": True} if p.get("oversize") else {})) for p in parts])
    _kv(L, 0, "index_file", "%s__index.csv" % stem)
    L.append("")
    L.append("batch:")
    for k in ("id", "name", "description", "tags", "input_file", "input_sha256", "submitted_by",
              "submitted_at", "started_at", "finished_at", "wall_clock_s", "status", "budget", "run_settings"):
        _kv(L, 2, k, manifest.get(k) if k != "status" or not partial_at else "partial")
    L.append("")
    L.append("engine:")
    eng = manifest.get("engine") or {}
    for k in ("channel", "path", "rudi_release", "arena_release", "personality_default"):
        _kv(L, 2, k, eng.get(k))
    _kv(L, 2, "prompt_assets", eng.get("prompt_assets") or {})
    L.append("  cascades:")
    for role in ("rudi", "tester", "judge"):
        _list_of_flows(L, 4, role, [{"model": e.get("name"), "key": e.get("key"),
                                     "eur_per_mtok": {"in": e.get("in"), "out": e.get("out")}, "price": e.get("basis")}
                                    for e in (eng.get("cascades") or {}).get(role) or [] if e.get("on", True)])
    _kv(L, 2, "evaluators", eng.get("evaluators") or ["llm-judge"])
    L.append("")
    L.append("summary:")
    for k, v in summary.items():
        _kv(L, 2, k, v)
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- planning
def plan_parts(rows, stem, budget_tokens, header_tokens, partial_at=None):
    """Split rows (input order, each with 'seq', 'id' and 'size' in characters) into parts.

    Whole cases only. A part closes before it would pass `budget_tokens`, counting its header. A
    single case bigger than the budget gets a part of its own, flagged oversize."""
    room = max(1, int(budget_tokens) - int(header_tokens))
    parts, cur, cur_tokens = [], [], 0
    for r in rows:
        t = est_tokens(int(r.get("size") or 0))
        if cur and cur_tokens + t > room:
            parts.append((cur, cur_tokens))
            cur, cur_tokens = [], 0
        cur.append(r)
        cur_tokens += t
    if cur:
        parts.append((cur, cur_tokens))
    total = len(parts)
    out = []
    for i, (items, tokens) in enumerate(parts, start=1):
        outcomes = {}
        for r in items:
            outcomes[r.get("outcome")] = outcomes.get(r.get("outcome"), 0) + 1
        out.append({"index": i, "seqs": [r["seq"] for r in items], "first_id": items[0]["id"], "last_id": items[-1]["id"],
                    "first": items[0]["id"].rsplit("-", 1)[-1], "last": items[-1]["id"].rsplit("-", 1)[-1],
                    "est_tokens": tokens + int(header_tokens), "outcomes": outcomes,
                    "oversize": len(items) == 1 and tokens > room,
                    "file": _results_name(stem, i, total, partial_at)})
    return out


def _results_name(stem, index, total, partial_at):
    from arena_common import results_name
    return results_name(stem, index, total, partial_at)


INDEX_COLUMNS = ["case_id", "key", "mode", "locale", "persona", "outcome", "stop_reason", "user_turns",
                 "duration_s", "errors", "cost_eur", "cap_eur", "part"]


def index_csv(rows, part_of):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(INDEX_COLUMNS)
    for r in rows:
        w.writerow([r["id"], r.get("key"), r.get("mode"), r.get("loc"), r.get("persona"), r.get("outcome"), r.get("stop"),
                    r.get("turns") or 0, r.get("dur") or 0, r.get("errs") or 0, "%.5f" % float(r.get("cost") or 0),
                    "%.2f" % float(r.get("cap") or 0), part_of.get(r["seq"], "")])
    return buf.getvalue()


def stub_record(row, manifest):
    """The case document for a case that never ran (cancelled, or the batch budget ran out)."""
    why = {"batch_budget": "Not started: the batch budget was spent.",
           "cancelled": "Not started: the batch was cancelled."}.get(row.get("stop"), "Not started.")
    return {"case": {"id": row["id"], "key": row.get("key"), "title": row.get("title") or "", "mode": row.get("mode"),
                     "channel": "chat", "locale": {"requested": row.get("loc"), "detected": None},
                     "persona": {"display_name": row.get("persona")}, "tags": row.get("tags") or []},
            "result": {"outcome": row.get("outcome"), "stop_reason": row.get("stop"), "verdict": why, "checks": [],
                       "counts": {"user_turns": 0, "rudi_turns": 0, "attempts": 0, "retries": 0},
                       "cost": {"eur": 0.0, "cap_eur": row.get("cap")}, "timing": {}, "final_state": {}, "errors": []},
            "transcript": []}
