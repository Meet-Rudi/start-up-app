"""
Input format tests (rudi-arena/input@1): the sample batch validates, every documented rule fires,
budgets resolve, identifiers are refused, JSON / JSONL read like YAML, and the results renderer
and part planner keep their promises. Synthetic data only.

Run:  python -m unittest discover -s services/training-arena/tests -v
"""

import os
import json
import unittest

import yaml

import fakes  # noqa: F401 - sets sys.path
import arena_format
import arena_results
import arena_common
from arena_store import ENGINE_DEFAULTS, BUDGET_DEFAULTS

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
SAMPLE = os.path.join(ROOT, "site", "training-arena", "samples", "TA-20261013-K7QX__goal-setting-smoke__input.yaml")
CRISIS = os.path.join(ROOT, "evals", "arena-batches", "crisis-signposting__input.yaml")


def validate(text, filename="x.yaml", **kw):
    v = arena_format.Validator(dict(ENGINE_DEFAULTS), dict(BUDGET_DEFAULTS), fakes.cascades(), **kw)
    specs = []
    report = v.run(arena_format.documents(text, filename), on_case=specs.append)
    return report, specs


HEADER = "format: rudi-arena/input@1\n"


def issues(report, sev="e"):
    return [m for s, _line, m in report["issues"] if s == sev]


class SampleTests(unittest.TestCase):
    def test_the_sample_batch_validates(self):
        with open(SAMPLE, encoding="utf-8") as fh:
            report, specs = validate(fh.read())
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["cases"], 7)
        self.assertEqual(report["by_mode"], {"scripted": 3, "intent": 3, "hybrid": 1})
        self.assertEqual(report["budget"], {"batch": 2.0, "batch_src": "the file", "case": 0.05, "case_src": "the file"})
        probe = next(s for s in specs if s["key"] == "en-dosage-probe")
        self.assertEqual(probe["limits"]["max_cost_eur"], 0.03)          # its own ceiling wins
        self.assertEqual(probe["stop"]["on_limit"], "pass")
        gap = next(s for s in specs if s["key"] == "en-commit-then-next-day")
        self.assertEqual(gap["script"][3]["gap_s"], 22 * 3600)
        self.assertGreater(report["cost_eur"], 0)

    def test_the_crisis_batch_validates(self):
        with open(CRISIS, encoding="utf-8") as fh:
            report, specs = validate(fh.read())
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["cases"], 4)
        self.assertEqual(report["by_mode"], {"scripted": 2, "intent": 2})
        for s in specs:
            if s["mode"] == "intent":
                self.assertEqual(s["stop"]["max_turns"], 5)
                self.assertTrue(s["stop"]["success_when"])
            else:
                self.assertEqual(s["expect"], [])                       # observation only: no criteria

    def test_line_numbers_point_at_the_case(self):
        text = HEADER + "---\ncase:\n  key: a\n  mode: scripted\n  script: [hi]\n---\ncase:\n  key: b\n"
        report, _ = validate(text)
        line = [ln for s, ln, m in report["issues"] if "mode" in m][0]
        self.assertEqual(line, 8)                                  # the second case's "case:" line


class RuleTests(unittest.TestCase):
    def test_header_and_empty(self):
        self.assertIn("Header is missing", " ".join(issues(validate("batch: {}\n---\ncase: {key: a, mode: scripted, script: [hi]}\n")[0])))
        self.assertIn("No test cases", " ".join(issues(validate(HEADER)[0])))

    def test_modes_need_their_parts(self):
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: scripted}\n---\ncase: {key: b, mode: intent}\n---\ncase: {key: c, mode: chatty, script: [x]}\n")
        text = " ".join(issues(r))
        self.assertIn("a scripted case needs a “script”", text)
        self.assertIn("needs “intent.objective”", text)
        self.assertIn("unknown mode", text)

    def test_intent_needs_a_way_to_succeed(self):
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: intent, intent: {objective: chat}}\n")
        self.assertIn("could only ever fail", " ".join(issues(r)))
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: intent, intent: {objective: chat}, stop: {on_limit: pass}}\n")
        self.assertTrue(r["ok"], r["issues"])

    def test_duplicates_reserved_and_limits(self):
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: scripted, script: [hi]}\n---\ncase: {key: a, mode: scripted, script: [hi]}\n"
                        "---\ncase: {key: c, mode: scripted, channel: call, script: [hi]}\n"
                        "---\ncase: {key: d, mode: scripted, rudi: {engine: v2}, script: [hi]}\n"
                        "---\ncase: {key: e, mode: scripted, limits: {max_turns: 99}, script: [hi]}\n"
                        "---\ncase: {key: f, mode: scripted, locale: fr, script: [hi]}\n")
        text = " ".join(issues(r))
        for needle in ("Duplicate key", "reserved for a later version", "rudi.engine", "over the arena cap", "not supported"):
            self.assertIn(needle, text)

    def test_script_entries(self):
        r, _ = validate(HEADER + "---\ncase:\n  key: a\n  mode: scripted\n  script:\n    - text: ''\n    - {text: hi, gap: soon}\n"
                        "    - {text: hi, expect_phase: dancing}\n    - {text: hi, media: [x]}\n")
        text = " ".join(issues(r))
        for needle in ("is empty", "unreadable gap", "unknown expect_phase", "media is reserved"):
            self.assertIn(needle, text)

    def test_missing_key_is_a_warning_and_auto_named(self):
        r, specs = validate(HEADER + "---\ncase: {mode: scripted, script: [hi]}\n")
        self.assertTrue(r["ok"])
        self.assertEqual(specs[0]["key"], "case-0001")
        self.assertIn("no “key”", " ".join(issues(r, "w")))

    def test_identifiers_are_errors(self):
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: scripted, script: ['mail me: tom@example.org', 'call +32 470 12 34 56']}\n")
        text = " ".join(issues(r))
        self.assertIn("email-address", text)
        self.assertIn("phone-number", text)
        self.assertFalse(r["ok"])

    def test_short_codes_are_not_identifiers(self):
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: scripted, script: ['is 1813 or 112 the number?']}\n")
        self.assertTrue(r["ok"], r["issues"])

    def test_unknown_personality_and_tester_model(self):
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: scripted, rudi: {personality: nope}, tester: {model: gpt-9}, script: [hi]}\n",
                        personalities=["seed-rudi-v2"])
        text = " ".join(issues(r))
        self.assertIn("unknown Rudi personality", text)
        self.assertIn("not in the arena's tester cascade", text)

    def test_named_evaluator_must_exist(self):
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: scripted, script: [hi], expect: [{text: ok, evaluator: nope}]}\n",
                        evaluators={"llm-judge"})
        self.assertIn("unknown evaluator", " ".join(issues(r)))


class BudgetTests(unittest.TestCase):
    def test_defaults_apply_when_the_file_is_silent(self):
        r, specs = validate(HEADER + "---\ncase: {key: a, mode: scripted, script: [hi]}\n")
        self.assertEqual(r["budget"]["batch_src"], "arena default")
        self.assertEqual(specs[0]["limits"]["max_cost_eur"], BUDGET_DEFAULTS["case_default"])

    def test_ceilings(self):
        r, _ = validate("format: rudi-arena/input@1\nbatch: {max_cost_eur: 250}\n---\ncase: {key: a, mode: scripted, script: [hi]}\n")
        self.assertIn("above the highest a file may set", " ".join(issues(r)))
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: scripted, limits: {max_cost_eur: 0.9}, script: [hi]}\n")
        self.assertIn("per case", " ".join(issues(r)))
        r, _ = validate("format: rudi-arena/input@1\nbatch: {max_cost_eur: 0.02}\n---\ncase: {key: a, mode: scripted, limits: {max_cost_eur: 0.04}, script: [hi]}\n")
        self.assertIn("above the whole batch budget", " ".join(issues(r)))

    def test_estimate_over_budget_warns(self):
        cases = "".join("---\ncase: {key: c%d, mode: intent, intent: {objective: go}, stop: {on_limit: pass, max_turns: 30}}\n" % i for i in range(30))
        r, _ = validate("format: rudi-arena/input@1\nbatch: {max_cost_eur: 0.05}\ndefaults: {limits: {max_cost_eur: 0.04}}\n" + cases)
        self.assertIn("would not run", " ".join(issues(r, "w")))


class OtherFormatsTests(unittest.TestCase):
    def test_json_and_jsonl(self):
        doc = {"header": {"format": "rudi-arena/input@1"}, "cases": [{"case": {"key": "a", "mode": "scripted", "script": ["hi"]}}]}
        r, _ = validate(json.dumps(doc), "b.json")
        self.assertTrue(r["ok"], r["issues"])
        lines = json.dumps(doc["header"]) + "\n" + json.dumps(doc["cases"][0]) + "\n"
        r, _ = validate(lines, "b.jsonl")
        self.assertTrue(r["ok"], r["issues"])

    def test_broken_yaml_is_reported_not_raised(self):
        r, _ = validate(HEADER + "---\ncase: {key: a, mode: [\n")
        self.assertFalse(r["ok"])
        self.assertIn("Not valid YAML", " ".join(issues(r)))

    def test_gap_parser(self):
        self.assertEqual(arena_format.parse_gap("1h30m"), 5400)
        self.assertEqual(arena_format.parse_gap("2d"), 172800)
        self.assertEqual(arena_format.parse_gap(None), 0)
        with self.assertRaises(ValueError):
            arena_format.parse_gap("tomorrow")


class ResultsTests(unittest.TestCase):
    def _record(self, n, text="Hi"):
        return {"case": {"id": "TA-20261014-AB12-%04d" % n, "key": "k%d" % n, "title": "Case: with colon", "mode": "intent",
                         "channel": "chat", "locale": {"requested": "nl", "detected": "nl"}, "persona": {"id": "x", "display_name": "Els"},
                         "rudi": {"engine": "live"}, "tags": ["a", "b"]},
                "result": {"outcome": "success", "stop_reason": "success_criterion", "verdict": "Met: yes — 'quoted' \"too\"",
                           "checks": [{"check": "x: y", "kind": "success_when", "result": "pass", "cost_eur": 0.0002}],
                           "counts": {"user_turns": 1}, "cost": {"eur": 0.001, "by_role": {"rudi": 0.0005, "infra": 0.00035}},
                           "timing": {"started_at": "2026-10-14T08:00:00.000Z"}, "final_state": {"phase": "commit"}, "errors": []},
                "transcript": [{"turn": 0, "from": "rudi", "at": "2026-10-14T08:00:00.000Z", "text": "👋 Hoi! Ik ben Rudi.",
                                "meta": {"phase": "learn", "model": "none"}},
                               {"turn": 1, "from": "user", "at": "2026-10-14T08:00:01.000Z", "text": text, "meta": {"source": "tester"}}]}

    def test_case_documents_are_valid_yaml_and_round_trip(self):
        for text in ("Hi", "yes", "null", "a: b", "- dash", "# hash", "multi\nline", "1813", "é ü 🙂"):
            doc = arena_results.render_case(self._record(1, text))
            loaded = list(yaml.safe_load_all(doc))[0]
            self.assertEqual(loaded["transcript"][1]["text"], text)
            self.assertEqual(loaded["result"]["verdict"], "Met: yes — 'quoted' \"too\"")
            self.assertEqual(loaded["case"]["title"], "Case: with colon")

    def test_plan_never_cuts_a_case_and_numbers_are_final(self):
        rows = [{"seq": i, "id": "TA-20261014-AB12-%04d" % i, "size": 32000 if i != 5 else 400000, "outcome": "success"} for i in range(1, 12)]
        plan = arena_results.plan_parts(rows, "TA-20261014-AB12__x", 30000, 2000)
        seqs = [s for p in plan for s in p["seqs"]]
        self.assertEqual(seqs, list(range(1, 12)))                       # every case once, in order
        big = [p for p in plan if 5 in p["seqs"]][0]
        self.assertEqual(big["seqs"], [5])
        self.assertTrue(big["oversize"])
        n = len(plan)
        self.assertTrue(all(p["file"].endswith("of%02d.yaml" % n) for p in plan))
        for p in plan:
            if not p["oversize"]:
                self.assertLessEqual(p["est_tokens"], 30000)

    def test_header_is_valid_yaml(self):
        rows = [{"seq": 1, "id": "TA-20261014-AB12-0001", "key": "k1", "outcome": "success", "stop": "script_end", "cost": 0.001, "size": 100}]
        plan = arena_results.plan_parts(rows, "TA-20261014-AB12", 450000, 2000)
        m = {"stem": "TA-20261014-AB12", "id": "TA-20261014-AB12", "engine": {"cascades": fakes.cascades()}}
        head = arena_results.render_header(m, plan[0], plan, {"cases": {"total": 1}}, rows)
        obj = yaml.safe_load(head)
        self.assertEqual(obj["part"]["of"], 1)
        self.assertEqual(obj["engine"]["cascades"]["tester"][0]["model"], "tester-groq-qwen3.8-27b")

    def test_names(self):
        self.assertEqual(arena_common.slug("Goal setting smoke!"), "goal-setting-smoke")
        self.assertEqual(arena_common.results_name("TA-20261014-AB12__x", 2, 3), "TA-20261014-AB12__x__results__p02of03.yaml")
        self.assertEqual(arena_common.results_name("S", 7, 120), "S__results__p007of120.yaml")
        self.assertEqual(arena_common.case_id("TA-20261014-AB12", 7, 12000), "TA-20261014-AB12-00007")
        self.assertRegex(arena_common.new_batch_id(), r"^TA-\d{8}-[0-9A-HJKMNP-TV-Z]{4}$")


if __name__ == "__main__":
    unittest.main()
