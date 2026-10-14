"""
MEET_RUDI Training Arena — the batch INPUT format (rudi-arena/input@1): read, check, resolve.

Spec: docs/training-arena-formats.md §3. One YAML stream — a header document, then one document
per case — or the JSON / JSONL equivalent. The stream is read one document at a time, so an input
file has no size limit: `validate()` hands every resolved case to a callback and keeps only
counters and the (capped) list of issues.

Nothing here talks to AWS. Identifier scanning is a callback (the arena passes the deployed
engine's own detector through meetrudi-engine-turn); costs come from arena_costs.
"""

import io
import re
import json

import yaml

import arena_costs

FORMAT_IN = "rudi-arena/input@1"
MODES = ("scripted", "intent", "hybrid")
PHASES = ("learn", "goal", "commit", "committed", "concluded")
LOCALES = ("en", "nl")
CHANNELS = ("chat",)
RESERVED_CHANNELS = ("call", "whatsapp")
ON_LIMIT = ("fail", "pass")
MAX_TEXT = 4000
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
GAP_RE = re.compile(r"^(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$", re.I)
CASE_FIELDS = {"key", "title", "mode", "persona", "locale", "channel", "rudi", "tester", "limits",
               "stop_on_rudi_conclude", "tags", "script", "expect", "intent", "stop"}

ARENA_DEFAULTS = {
    "channel": "chat", "locale": "en",
    "rudi": {"engine": "live", "personality": ""},
    "tester": {"model": "default", "temperature": None},
    "limits": {"max_turns": 12, "turn_timeout_s": 60, "max_cost_eur": None},
    "stop_on_rudi_conclude": True,
}


class FormatError(ValueError):
    """The file cannot be read at all (not YAML/JSON, or broken mid-way)."""

    def __init__(self, message, line=1):
        super().__init__(message)
        self.line = line


# --------------------------------------------------------------------------- reading
def documents(source, filename="input.yaml"):
    """Yield (line, document) one at a time. `source` is text, bytes or a file-like object."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "yaml"
    if ext in ("yaml", "yml"):
        yield from _yaml_documents(source)
    elif ext == "json":
        yield from _json_documents(source)
    elif ext == "jsonl":
        yield from _jsonl_documents(source)
    else:
        raise FormatError("Unsupported file type .%s — use .yaml, .yml, .json or .jsonl" % ext)


def _yaml_documents(source):
    loader = yaml.SafeLoader(source)
    try:
        while True:
            try:
                if not loader.check_node():
                    return
                node = loader.get_node()
                doc = loader.construct_document(node)
            except yaml.YAMLError as e:
                mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
                raise FormatError("Not valid YAML: %s" % getattr(e, "problem", e), (mark.line + 1) if mark else 1)
            if node is not None:
                yield node.start_mark.line + 1, doc
    finally:
        loader.dispose()


def _read_all(source):
    if hasattr(source, "read"):
        source = source.read()
    return source.decode("utf-8") if isinstance(source, bytes) else source


def _json_documents(source):
    try:
        obj = json.loads(_read_all(source))
    except ValueError as e:
        raise FormatError("Not valid JSON: %s" % e, getattr(e, "lineno", 1))
    if not isinstance(obj, dict) or "header" not in obj or not isinstance(obj.get("cases"), list):
        raise FormatError('A .json batch is one object: {"header": {…}, "cases": [{"case": {…}}, …]}')
    yield 1, obj["header"]
    for c in obj["cases"]:
        yield 1, c


def _jsonl_documents(source):
    stream = source if hasattr(source, "read") else io.BytesIO(source.encode("utf-8") if isinstance(source, str) else source)
    for n, raw in enumerate(_lines(stream), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            yield n, json.loads(line)
        except ValueError as e:
            raise FormatError("Line %d is not valid JSON: %s" % (n, e), n)


def _lines(stream):
    buf = b""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        buf += chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8")
    if buf:
        yield buf.decode("utf-8")


# --------------------------------------------------------------------------- small parsers
def parse_gap(value):
    """"22h" → 79200 seconds; None for nothing; raises ValueError when unreadable."""
    if value in (None, "", 0):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    m = GAP_RE.match(str(value).strip())
    if not m or not any(m.groups()):
        raise ValueError("unreadable gap %r (use e.g. 30m, 22h, 2d)" % value)
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def criterion(value, where):
    """A criterion is a sentence, or {text, evaluator}. Returns the normalised dict."""
    if isinstance(value, str) and value.strip():
        return {"text": value.strip(), "evaluator": "auto"}
    if isinstance(value, dict) and isinstance(value.get("text"), str) and value["text"].strip():
        return {"text": value["text"].strip(), "evaluator": str(value.get("evaluator") or "auto")}
    raise ValueError("%s must be a sentence, or {text, evaluator}" % where)


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# --------------------------------------------------------------------------- validation
class Report:
    def __init__(self, max_issues):
        self.max_issues, self.issues, self.errors, self.warnings = max_issues, [], 0, 0

    def add(self, sev, line, msg):
        if sev == "e":
            self.errors += 1
        else:
            self.warnings += 1
        if len(self.issues) < self.max_issues:
            self.issues.append([sev, int(line or 1), msg])


class Validator:
    """Checks a batch input and resolves every case.

    settings    engine settings (max_turns_cap, daily_quota, think_time, max_parallel)
    budgets     budget settings (case_default, batch_default, case_ceiling, batch_ceiling, infra)
    cascades    {rudi, tester, judge} entries — for tester-model checks and cost estimates
    personalities  slugs Rudi knows (None = do not check)
    evaluators  evaluator names that may be named in a criterion (None = do not check)
    scan        callable(list of texts) → list of {label: count}; the engine's identifier detector
    """

    def __init__(self, settings, budgets, cascades, personalities=None, evaluators=None, scan=None,
                 max_issues=500, only_keys=None):
        self.s, self.b, self.cascades = settings, budgets, cascades
        self.personalities, self.evaluators, self.scan = personalities, evaluators, scan
        self.only_keys = set(only_keys) if only_keys else None
        self.report = Report(max_issues)
        self.prices = arena_costs.estimate_model(cascades, budgets)

    def run(self, docs, on_case=None):
        r = self.report
        header, seq, seen = None, 0, {}
        stats = {"cases": 0, "by_mode": {}, "by_locale": {}, "by_persona": {}, "turns": 0,
                 "rudi_replies": 0, "tester_calls": 0, "judge_calls": 0, "llm_calls": 0, "cost_eur": 0.0}
        pending_scan = []
        try:
            for line, doc in docs:
                if header is None:
                    header = self._header(line, doc)
                    continue
                spec = self._case(line, doc, header, seq + 1, seen)
                if spec is None:
                    continue
                if self.only_keys is not None and spec["key"] not in self.only_keys:
                    continue
                seq += 1
                spec["seq"] = seq
                pending_scan += [(line, t) for t in _texts(spec)]
                if len(pending_scan) >= 400:
                    self._scan(pending_scan)
                    pending_scan = []
                est = arena_costs.estimate_case(spec, self.prices)
                spec["estimate"] = est
                if est["eur"] > spec["limits"]["max_cost_eur"]:
                    r.add("w", line, "%s: at worst €%.4f, above its €%.2f ceiling — it may stop as over budget."
                          % (spec["key"], est["eur"], spec["limits"]["max_cost_eur"]))
                for k, v in (("by_mode", spec["mode"]), ("by_locale", spec["locale"]),
                             ("by_persona", (spec.get("persona") or {}).get("display_name") or "(none)")):
                    stats[k][v] = stats[k].get(v, 0) + 1
                stats["cases"] += 1
                stats["turns"] += est["turns"]
                stats["rudi_replies"] += est["turns"] + 1
                stats["tester_calls"] += est["tester_calls"]
                stats["judge_calls"] += est["judge_calls"]
                stats["llm_calls"] += est["calls"]
                stats["cost_eur"] += est["eur"]
                if on_case is not None and r.errors == 0:
                    on_case(spec)
        except FormatError as e:
            r.add("e", e.line, str(e))
        if pending_scan:
            self._scan(pending_scan)
        if header is None and not r.errors:
            r.add("e", 1, "The file is empty.")
        if header is not None and stats["cases"] == 0:
            r.add("e", 1, "No test cases found. Each case is its own document, after a line with three dashes.")
        budget = self._budget(header or {})
        if stats["cases"] and stats["cost_eur"] > budget["batch"]:
            short = int(stats["cases"] * (1 - budget["batch"] / stats["cost_eur"])) + 1
            r.add("w", 1, "Estimated €%.2f is above the batch budget of €%.2f — roughly %d cases would not run."
                  % (stats["cost_eur"], budget["batch"], short))
        quota = int(self.s.get("daily_quota") or 0)
        if quota and stats["cases"] > quota:
            r.add("w", 1, "%d cases is more than the daily quota of %d per user; the batch will run over several days."
                  % (stats["cases"], quota))
        parallel = max(1, int(self.s.get("max_parallel") or 1))
        stats["duration_s"] = int(stats["turns"] * (5 + float(self.s.get("think_time") or 0)) / parallel)
        stats["cost_eur"] = round(stats["cost_eur"], 6)
        hb = (header or {}).get("batch") if isinstance((header or {}).get("batch"), dict) else {}
        return {"ok": r.errors == 0 and stats["cases"] > 0, "errors": r.errors, "warnings": r.warnings,
                "issues": r.issues, "budget": budget, "batch": {"name": hb.get("name") or "", "description": hb.get("description") or "",
                                                                 "tags": hb.get("tags") or []}, **stats}

    # -- header ---------------------------------------------------------------------------------
    def _header(self, line, doc):
        r = self.report
        if not isinstance(doc, dict):
            r.add("e", line, "The first document must be the header (a mapping with “format: %s”)." % FORMAT_IN)
            return {}
        if doc.get("format") != FORMAT_IN:
            r.add("e", line, "Header is missing “format: %s”." % FORMAT_IN)
        batch = doc.get("batch") if isinstance(doc.get("batch"), dict) else {}
        cap = batch.get("max_cost_eur")
        if cap is not None:
            if _num(cap) is None or cap <= 0:
                r.add("e", line, "batch.max_cost_eur must be a positive number of euro.")
            elif cap > float(self.b["batch_ceiling"]):
                r.add("e", line, "batch.max_cost_eur €%.2f is above the highest a file may set (€%.2f)."
                      % (cap, float(self.b["batch_ceiling"])))
        personas = doc.get("personas") or {}
        if not isinstance(personas, dict):
            r.add("e", line, "personas must be a mapping of id → persona.")
            personas = {}
        defaults = doc.get("defaults") or {}
        if not isinstance(defaults, dict):
            r.add("e", line, "defaults must be a mapping.")
            defaults = {}
        return {"format": doc.get("format"), "batch": batch, "personas": personas, "defaults": defaults, "line": line}

    def _budget(self, header):
        b = self.b
        batch = (header.get("batch") or {}).get("max_cost_eur")
        case = ((header.get("defaults") or {}).get("limits") or {}).get("max_cost_eur")
        return {"batch": float(batch) if _num(batch) else float(b["batch_default"]),
                "batch_src": "the file" if _num(batch) else "arena default",
                "case": float(case) if _num(case) else float(b["case_default"]),
                "case_src": "the file" if _num(case) else "arena default"}

    # -- case -----------------------------------------------------------------------------------
    def _case(self, line, doc, header, seq, seen):
        r = self.report
        if not isinstance(doc, dict) or not isinstance(doc.get("case"), dict):
            if doc not in (None, {}):
                r.add("e", line, "Document at line %d is not a case (expected a top-level “case:” key)." % line)
            return None
        c = doc["case"]
        key = c.get("key")
        if key is None:
            key = "case-%04d" % seq
            r.add("w", line, "Case %d has no “key”; it will be called %s." % (seq, key))
        elif not isinstance(key, str) or not KEY_RE.match(key):
            r.add("e", line, "Key %r: use a–z, 0–9 and dashes, at most 60 characters." % key)
            key = str(key)
        if key in seen:
            r.add("e", line, "Duplicate key “%s” (first used at line %d)." % (key, seen[key]))
        seen.setdefault(key, line)
        for extra in sorted(set(c) - CASE_FIELDS):
            r.add("w", line, "%s: field “%s” is not part of the format and is ignored." % (key, extra))

        spec = _merge(ARENA_DEFAULTS, header.get("defaults") or {})
        persona = self._persona(line, key, c.get("persona"), header.get("personas") or {})
        if persona and persona.get("locale") and "locale" not in c:
            spec["locale"] = persona["locale"]
        spec = _merge(spec, {k: v for k, v in c.items() if k in ("locale", "channel", "rudi", "tester", "limits", "stop_on_rudi_conclude")})
        spec.update({"key": key, "title": str(c.get("title") or ""), "mode": c.get("mode"),
                     "persona": persona, "tags": [str(t) for t in (c.get("tags") or [])], "line": line})
        if persona and persona.get("locale") and c.get("locale") and persona["locale"] != c["locale"]:
            r.add("w", line, "%s: the persona speaks “%s” but the case is set to “%s”." % (key, persona["locale"], c["locale"]))

        mode = spec["mode"]
        if mode is None:
            r.add("e", line, "%s: “mode” is required (scripted, intent or hybrid)." % key)
        elif mode not in MODES:
            r.add("e", line, "%s: unknown mode “%s”." % (key, mode))
        if spec["channel"] in RESERVED_CHANNELS:
            r.add("e", line, "%s: channel “%s” is reserved for a later version; v1 runs chat only." % (key, spec["channel"]))
        elif spec["channel"] not in CHANNELS:
            r.add("e", line, "%s: unknown channel “%s”." % (key, spec["channel"]))
        if (spec.get("rudi") or {}).get("engine", "live") != "live":
            r.add("e", line, "%s: rudi.engine “%s” is reserved; v1 runs the live engine only." % (key, spec["rudi"]["engine"]))
        slug = (spec.get("rudi") or {}).get("personality") or ""
        if slug and self.personalities is not None and slug not in self.personalities:
            r.add("e", line, "%s: unknown Rudi personality “%s”." % (key, slug))
        if spec["locale"] not in LOCALES:
            r.add("e", line, "%s: locale “%s” is not supported (en, nl)." % (key, spec["locale"]))
        tm = (spec.get("tester") or {}).get("model") or "default"
        names = [e.get("name") for e in (self.cascades.get("tester") or [])]
        if tm != "default" and tm not in names:
            r.add("e", line, "%s: tester.model “%s” is not in the arena's tester cascade." % (key, tm))

        limits = spec["limits"]
        cap_turns = int(self.s.get("max_turns_cap") or 40)
        if not isinstance(limits.get("max_turns"), int) or limits["max_turns"] < 1:
            r.add("e", line, "%s: limits.max_turns must be a whole number from 1." % key)
            limits["max_turns"] = 12
        elif limits["max_turns"] > cap_turns:
            r.add("e", line, "%s: max_turns %d is over the arena cap of %d." % (key, limits["max_turns"], cap_turns))
        if _num(limits.get("turn_timeout_s")) is None or not 10 <= limits["turn_timeout_s"] <= 300:
            r.add("e", line, "%s: limits.turn_timeout_s must be between 10 and 300 seconds." % key)
            limits["turn_timeout_s"] = 60
        own = limits.get("max_cost_eur")
        if own is None:
            limits["max_cost_eur"] = float(self.b["case_default"]) if not _num(((header.get("defaults") or {}).get("limits") or {}).get("max_cost_eur")) \
                else float(header["defaults"]["limits"]["max_cost_eur"])
        elif _num(own) is None or own <= 0:
            r.add("e", line, "%s: limits.max_cost_eur must be a positive number of euro." % key)
            limits["max_cost_eur"] = float(self.b["case_default"])
        elif own > float(self.b["case_ceiling"]):
            r.add("e", line, "%s: limits.max_cost_eur €%.2f is above the highest a file may set per case (€%.2f)."
                  % (key, own, float(self.b["case_ceiling"])))
        batch_cap = self._budget(header)["batch"]
        if limits["max_cost_eur"] > batch_cap:
            r.add("e", line, "%s: its ceiling (€%.2f) is above the whole batch budget (€%.2f)." % (key, limits["max_cost_eur"], batch_cap))

        spec["script"] = self._script(line, key, c.get("script"), mode)
        spec["expect"] = self._criteria(line, key, c.get("expect"), "expect")
        spec["intent"] = self._intent(line, key, c.get("intent"), mode)
        spec["stop"] = self._stop(line, key, c.get("stop"), mode, cap_turns)
        if spec["mode"] == "scripted" and len(spec["script"]) > limits["max_turns"]:
            r.add("w", line, "%s: the script has %d messages but max_turns is %d; the rest will not be sent."
                  % (key, len(spec["script"]), limits["max_turns"]))
        return spec

    def _persona(self, line, key, value, personas):
        r = self.report
        if value is None:
            return None
        if isinstance(value, str):
            p = personas.get(value)
            if not isinstance(p, dict):
                r.add("e", line, "%s: unknown persona “%s”." % (key, value))
                return None
            p = dict(p, id=value)
        elif isinstance(value, dict):
            p = dict(value, id="inline")
        else:
            r.add("e", line, "%s: persona must be an id or an inline persona." % key)
            return None
        if "voice" in p:
            r.add("e", line, "%s: persona.voice is reserved for call simulation (v2)." % key)
        p.setdefault("display_name", p["id"].capitalize() if p["id"] != "inline" else "User")
        if p.get("locale") and p["locale"] not in LOCALES:
            r.add("e", line, "%s: persona locale “%s” is not supported." % (key, p["locale"]))
        return p

    def _script(self, line, key, value, mode):
        r = self.report
        if mode in ("scripted", "hybrid") and not value:
            r.add("e", line, "%s: a %s case needs a “script”." % (key, mode))
            return []
        if value is None:
            return []
        if not isinstance(value, list):
            r.add("e", line, "%s: script must be a list of messages." % key)
            return []
        out = []
        for i, e in enumerate(value, start=1):
            entry = {"text": e} if isinstance(e, str) else e
            if not isinstance(entry, dict) or not isinstance(entry.get("text"), str):
                r.add("e", line, "%s: script message %d needs “text”." % (key, i))
                continue
            text = entry["text"].strip()
            if not text:
                r.add("e", line, "%s: script message %d is empty." % (key, i))
            elif len(text) > MAX_TEXT:
                r.add("e", line, "%s: script message %d is over %d characters." % (key, i, MAX_TEXT))
            if "media" in entry:
                r.add("e", line, "%s: script[].media is reserved for a later version." % key)
            try:
                gap = parse_gap(entry.get("gap"))
            except ValueError as ex:
                r.add("e", line, "%s: script message %d: %s." % (key, i, ex))
                gap = 0
            if gap and i == 1:
                r.add("w", line, "%s: a gap on the first message changes nothing." % key)
            phase = entry.get("expect_phase")
            if phase is not None and phase not in PHASES:
                r.add("e", line, "%s: script message %d: unknown expect_phase “%s”." % (key, i, phase))
            exp = None
            if entry.get("expect") is not None:
                try:
                    exp = criterion(entry["expect"], "script message %d expect" % i)
                    self._evaluator(line, key, exp)
                except ValueError as ex:
                    r.add("e", line, "%s: %s." % (key, ex))
            out.append({"text": text[:MAX_TEXT], "gap_s": gap, "gap": entry.get("gap"), "expect_phase": phase, "expect": exp})
        return out

    def _criteria(self, line, key, value, where):
        if value is None:
            return []
        items = value if isinstance(value, list) else [value]
        out = []
        for i, v in enumerate(items, start=1):
            try:
                c = criterion(v, "%s item %d" % (where, i))
                self._evaluator(line, key, c)
                out.append(c)
            except ValueError as ex:
                self.report.add("e", line, "%s: %s." % (key, ex))
        return out

    def _evaluator(self, line, key, crit):
        if crit["evaluator"] != "auto" and self.evaluators is not None and crit["evaluator"] not in self.evaluators:
            self.report.add("e", line, "%s: unknown evaluator “%s”." % (key, crit["evaluator"]))

    def _intent(self, line, key, value, mode):
        r = self.report
        if mode not in ("intent", "hybrid"):
            if value:
                r.add("w", line, "%s: “intent” is ignored in a scripted case." % key)
            return None
        if not isinstance(value, dict) or not isinstance(value.get("objective"), str) or not value["objective"].strip():
            r.add("e", line, "%s: a %s case needs “intent.objective”." % (key, mode))
            return None
        opening = value.get("opening")
        if opening is not None and (not isinstance(opening, str) or not opening.strip() or len(opening) > MAX_TEXT):
            r.add("e", line, "%s: intent.opening must be a message of at most %d characters." % (key, MAX_TEXT))
            opening = None
        reveal = value.get("reveal") or []
        if not isinstance(reveal, list):
            r.add("e", line, "%s: intent.reveal must be a list of facts." % key)
            reveal = []
        return {"objective": value["objective"].strip(), "opening": opening.strip() if opening else None,
                "reveal": [str(x) for x in reveal]}

    def _stop(self, line, key, value, mode, cap_turns):
        r = self.report
        value = value or {}
        if not isinstance(value, dict):
            r.add("e", line, "%s: stop must be a mapping." % key)
            value = {}
        out = {"success_when": None, "abort_when": None, "max_turns": None, "max_attempts": None, "on_limit": "fail"}
        for k in ("success_when", "abort_when"):
            if value.get(k) is not None:
                try:
                    out[k] = criterion(value[k], "stop.%s" % k)
                    self._evaluator(line, key, out[k])
                except ValueError as ex:
                    r.add("e", line, "%s: %s." % (key, ex))
        for k in ("max_turns", "max_attempts"):
            v = value.get(k)
            if v is not None:
                if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                    r.add("e", line, "%s: stop.%s must be a whole number from 1." % (key, k))
                elif k == "max_turns" and v > cap_turns:
                    r.add("e", line, "%s: max_turns %d is over the arena cap of %d." % (key, v, cap_turns))
                else:
                    out[k] = v
        if value.get("on_limit") is not None:
            if value["on_limit"] not in ON_LIMIT:
                r.add("e", line, "%s: stop.on_limit must be fail or pass." % key)
            else:
                out["on_limit"] = value["on_limit"]
        if mode in ("intent", "hybrid") and not (out["success_when"] or out["abort_when"] or out["on_limit"] == "pass"):
            r.add("e", line, "%s: no success_when, abort_when or on_limit: pass — the case could only ever fail." % key)
        return out

    def _scan(self, items):
        texts = [t for _, t in items]
        try:
            found = self.scan(texts) if self.scan else [local_identifiers(t) for t in texts]
        except Exception as e:  # noqa: BLE001 - a scan that cannot run must not wave the file through
            self.report.add("e", 1, "The identifier check could not run (%s); nothing was launched." % (str(e)[:160] or type(e).__name__))
            return
        for (line, _), labels in zip(items, found):
            for label in sorted(labels or {}):
                self.report.add("e", line, "Looks like a real identifier (%s). Inputs must be synthetic — remove it." % label)


def _texts(spec):
    """Every free text of a case — what the identifier check must see."""
    out = [spec.get("title") or ""]
    for e in spec.get("script") or []:
        out.append(e["text"])
        if e.get("expect"):
            out.append(e["expect"]["text"])
    out += [c["text"] for c in spec.get("expect") or []]
    intent = spec.get("intent") or {}
    out += [intent.get("objective") or "", intent.get("opening") or ""] + list(intent.get("reveal") or [])
    stop = spec.get("stop") or {}
    out += [(stop.get(k) or {}).get("text", "") for k in ("success_when", "abort_when")]
    p = spec.get("persona") or {}
    out += [str(p.get(k) or "") for k in ("display_name", "background", "attitude")]
    return [t for t in out if t]


_LOCAL_PATTERNS = (
    ("email-address", re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.I)),
    ("phone-number", re.compile(r"(?<!\d)\+?\d[\d ./-]{7,}\d(?!\d)")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[0-9A-Z]{4}){3,}\b")),
)


def local_identifiers(text):
    """Fallback detector for tests and offline checks. Live validation uses the engine's own."""
    found = {}
    for label, rx in _LOCAL_PATTERNS:
        n = len(rx.findall(text or ""))
        if n:
            found[label] = n
    return found
