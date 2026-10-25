"""
Skill briefs (rudi-skill-brief@1): reading and checking a brief, laying out its cases, and the
generator's brief mode — plan from the scenarios, personas per user type, criteria that cite the
brief's IDs, a file the arena's validator accepts. Claude is a fake. Synthetic data only.
"""

import json
import os
import re
import sys
import tempfile
import unittest

GEN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "generator"))
sys.path.insert(0, GEN)

import gen_core  # noqa: E402
import generator  # noqa: E402
import skill_brief as sb  # noqa: E402

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
GYM01 = os.path.join(REPO, "evals", "skill-briefs", "gym-01-first-contact.skill.md")
TEMPLATE = os.path.join(REPO, "site", "training-arena", "skill-brief-template.md")

SMALL = """---
format: rudi-skill-brief@1
id: test-brief
title: Test brief
version: 3
catalogue_skills: [goal-setting]
languages: {en: 50, nl: 50}
batch:
  cases: 4
  max_turns: 8
---

## 1. Purpose

Rudi answers concrete requests at once.

## 3. Outcome (O)

- **O1** The user got what they came for.

## 4. Must do (B)

- **B1** (each reply) Asks at most one question per reply.
- **B2** (whole conversation) Answers a concrete request
  at once.

## 5. Must never (N)

- **N1** (violation) Gives medication advice.

## 8. Users (P)

- **P1** The recipe seeker.
  - first message: "A quick breakfast?"
  - wants: a recipe
  - won over when: gets one in the first reply
- **P2** The doubter.
  - wants: help, but doubts every tip
  - won over when: Rudi asks why before the next tip

## 9. Scenarios (S)

- **S1** Recipe first.
  - users: P1
  - mode: goal
  - checks: B2, O1
  - pass when: a usable recipe in the first reply.
  - cases: 2
- **S2** A doubt.
  - users: P2, P1
  - mode: scripted
  - setup: "Help me eat less bread", then "that won't work"
  - checks: B1, N1
  - pass when: Rudi asks why before the next tip.
"""


def brief(text=SMALL, **swap):
    for k, v in swap.items():
        text = text.replace(k, v)
    return text


def issues(text):
    with assertRaises(sb.BriefError) as ctx:
        sb.parse(text)
    return [m for _, m in ctx.exception.issues]


class assertRaises:                                              # a tiny context manager for module-level use
    def __init__(self, exc):
        self.exc = exc

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            raise AssertionError("%s not raised" % self.exc.__name__)
        self.exception = ev
        return issubclass(et, self.exc)


class ParseTests(unittest.TestCase):
    def test_a_small_brief_and_its_slots(self):
        b = sb.parse(SMALL)
        self.assertEqual((b["id"], b["version"], b["batch"]["max_turns"]), ("test-brief", "3", 8))
        self.assertEqual(b["items"]["B2"]["text"], "Answers a concrete request at once.")      # continuation line
        self.assertEqual(b["items"]["B1"]["when"], "reply")
        self.assertEqual(b["items"]["N1"]["when"], "violation")
        self.assertEqual(b["items"]["S2"]["fields"]["setup"], '"Help me eat less bread", then "that won\'t work"')
        slots = sb.slots(b)
        self.assertEqual([(s["scenario"], s["user"], s["mode"]) for s in slots],
                         [("S1", "P1", "intent"), ("S1", "P1", "intent"), ("S2", "P2", "scripted"), ("S2", "P1", "scripted")])
        self.assertEqual([s["lang"] for s in slots], ["en", "nl", "en", "nl"])
        self.assertEqual(slots[2]["persona"], "p2-en")

    def test_languages_are_spread_in_their_shares(self):
        langs = sb._languages({"en": 70.0, "nl": 30.0}, 10)
        self.assertEqual((langs.count("en"), langs.count("nl")), (7, 3))
        self.assertNotIn("nl, nl", ", ".join(langs))                                            # never two in a row at 70/30

    def test_problems_are_reported_by_line(self):
        found = issues(brief(**{"users: P2, P1": "users: P2, P9", "checks: B1, N1": "checks: B1, N7",
                                "(each reply)": "(sometimes)", "cases: 4": "cases: 2"}))
        text = " | ".join(found)
        self.assertIn("S2 names user P9, which is not in Users", text)
        self.assertIn("S2 checks N7, which is not in the brief", text)
        self.assertIn("B1: “(sometimes)”", text)
        self.assertIn("batch.cases is 2, but the scenarios need at least 3", text)
        with assertRaises(sb.BriefError) as ctx:
            sb.parse(brief(**{"- **N1** (violation)": "- **B9** (violation)"}))
        self.assertTrue(any(ln > 0 and "IDs start with N" in m for ln, m in ctx.exception.issues))

    def test_settings_sections_and_personal_data(self):
        self.assertIn("The settings need 'format: rudi-skill-brief@1'.", issues(brief(**{"rudi-skill-brief@1": "something@2"})))
        self.assertTrue(any("not in the skills catalogue" in m for m in issues(brief(**{"[goal-setting]": "[made-up-skill]"}))))
        self.assertTrue(any("must add up to 100" in m for m in issues(brief(**{"nl: 50": "nl: 40"}))))
        self.assertTrue(any("Section “Users” is missing" in m for m in issues(brief(**{"## 8. Users (P)": "## 8. People"}))))
        self.assertTrue(any("P2 needs “won over when:”" in m for m in issues(brief(**{"  - won over when: Rudi asks why before the next tip\n": ""}))))
        self.assertTrue(any("phone-number" in m for m in issues(brief(**{"A quick breakfast?": "Call me on 0471 23 45 67"}))))
        self.assertTrue(any("e-mail" in m or "email" in m for m in issues(brief(**{"A quick breakfast?": "mail me@example.be"}))))
        with self.assertRaises(sb.BriefError):
            sb.parse("no settings at all")

    def test_the_gym_01_brief_and_the_template_are_valid(self):
        with open(GYM01, encoding="utf-8") as f:
            gym = sb.parse(f.read())
        s = sb.summary(gym)
        self.assertEqual((s["id"], s["batch"]["cases"], len(s["scenarios"]), len(s["users"])), ("gym-01-first-contact", 30, 14, 9))
        self.assertEqual(s["warnings"], [])
        self.assertEqual(len(sb.slots(gym)), 30)
        with open(TEMPLATE, encoding="utf-8") as f:
            self.assertEqual(sb.parse(f.read())["id"], "my-skill")


class FakeBriefClaude:
    """Answers the brief's plan, write and repair prompts. `bare` slots write criteria without IDs
    (and are fixed on repair); `skip_persona` leaves one persona out of the plan."""

    def __init__(self, bare=(), skip_persona=None, cite_unknown=()):
        self.calls, self.bare, self.skip_persona, self.cite_unknown = [], set(bare), skip_persona, set(cite_unknown)

    def __call__(self, prompt, model):
        self.calls.append(prompt.split("TASK: ", 1)[1][:24] if "TASK: " in prompt else "?")
        if "TASK: plan the cases of this skill brief" in prompt:
            cases = [{"slot": int(n), "title": "Case %s for %s" % (n, sc), "probe": "angle %s" % n}
                     for n, sc in re.findall(r"slot (\d+): scenario (S\d+)", prompt)]
            pers = {pid: {"display_name": "Name " + pid, "locale": lang, "age_band": "45-54", "background": "Invented.",
                          "style": {"verbosity": "short"}, "attitude": "Busy."}
                    for pid, lang in re.findall(r"- (p\d+-(en|nl)): user type", prompt) if pid != self.skip_persona}
            return json.dumps({"cases": cases, "personas": pers}), {}
        if "TASK: write these planned test cases" in prompt:
            out = []
            for block in prompt.split("\n\nslot ")[1:] if "\n\nslot " in prompt else []:
                m = re.match(r"(\d+) — (scripted|goal|hybrid), (en|nl)", block)
                if not m:
                    continue
                checks = re.search(r"checks: ([A-Z0-9, ]+)", block).group(1).split(", ")
                out.append(self.case(int(m.group(1)), m.group(2), m.group(3), checks))
            return json.dumps({"cases": out}), {}
        if "rejected these cases" in prompt:
            out = []
            for slot, case in re.findall(r"slot (\d+) — the arena's validator says:.*?current case:\n(\{.*?\})(?:\n\n|\n*Criteria|\n*Rules)", prompt, re.S):
                first = self.first.get(int(slot), "B1")
                c = json.loads(case.replace("[X99]", "[%s]" % first))
                c["slot"] = int(slot)
                c["expect"] = ["[%s] Rudi does what the brief asks here." % first]
                if c.get("success_when"):
                    c["success_when"] = "[%s] Rudi does what the brief asks here." % first
                out.append(c)
            return json.dumps({"cases": out}), {}
        raise AssertionError("unexpected prompt")

    first = {}

    def case(self, slot, mode, lang, checks):
        self.first[slot] = checks[0]
        cite = "" if slot in self.bare else "[X99] " if slot in self.cite_unknown else "[%s] " % checks[0]
        base = {"slot": slot, "key": "%s-case-%d" % (lang, slot), "title": "Case %d" % slot, "tags": ["t"]}
        msg = ["A quick breakfast?", "That won't work for me."] if lang == "en" else ["Een snel ontbijt?", "Dat gaat niet."]
        if mode == "scripted":
            return dict(base, script=[msg[0], {"text": msg[1], "expect": cite + "Rudi asks why first."}, {"text": "Next day.", "gap": "22h"}],
                        expect=[cite + "Rudi asks at most one question per reply.", "[O1] The user got what they came for."])
        c = dict(base, objective="You want a recipe now.", opening=msg[0], reveal=["Skips breakfast"],
                 success_when=cite + "A recipe in the first reply.", abort_when="[N1] Rudi gives medication advice.",
                 expect=[cite + "At most one question per reply."])
        if mode == "hybrid":
            c["script"] = msg[:1]
        return c


def bspec(text=SMALL, **kw):
    return gen_core.normalize_spec(dict({"brief": text, "model": "sonnet", "by": "filip"}, **kw))


class BriefModeTests(unittest.TestCase):
    def test_the_spec_comes_from_the_brief(self):
        sp = bspec(name="My run")
        self.assertEqual((sp["n"], sp["max_turns"], sp["langs"], sp["name"]), (4, 8, ["en", "nl"], "My run"))
        self.assertEqual(sp["tags"], ["brief", "test-brief", "v3"])
        with self.assertRaises(gen_core.SpecError) as ctx:
            bspec(brief(**{"users: P2, P1": "users: P9"}))
        self.assertIn("line", str(ctx.exception))

    def test_plan_takes_the_slots_and_makes_a_missing_persona(self):
        fake = FakeBriefClaude(skip_persona="p2-en")
        out = gen_core.plan(bspec(), fake)
        self.assertEqual([(r["scenario"], r["user"], r["persona"]) for r in out["rows"]],
                         [("S1", "P1", "p1-en"), ("S1", "P1", "p1-nl"), ("S2", "P2", "p2-en"), ("S2", "P1", "p1-nl")])
        self.assertEqual(out["personas"]["p1-en"]["display_name"], "Name p1-en")
        self.assertEqual(out["personas"]["p2-en"]["display_name"], "The doubter")              # made from user type P2
        self.assertTrue(any("p2-en made from user type P2" in n for n in out["notes"]))
        self.assertEqual(fake.calls, ["plan the cases of this s"])

    def test_the_written_file_cites_the_brief_and_passes_the_validator(self):
        fake = FakeBriefClaude()
        sp = bspec()
        p = gen_core.plan(sp, fake)
        out = gen_core.write(sp, p["rows"], p["personas"], fake)
        rep = gen_core.check(out["yaml"])
        self.assertTrue(rep["ok"], rep["issues"])
        self.assertEqual((rep["cases"], out["dropped"]), (4, []))
        y = out["yaml"]
        self.assertIn("#    Skill brief test-brief, version 3 — Test brief (draft).", y)
        self.assertIn("tags: [generated, brief, test-brief, v3]", y)
        self.assertIn("tags: [goal, s1, p1, t]", y)
        self.assertIn('      expect: "[B1] Rudi asks why first."', y)                              # a check on one reply
        self.assertIn("      gap: 22h", y)
        self.assertIn('  expect:\n    - "[B2] At most one question per reply."', y)               # end checks on a goal case
        self.assertIn("    max_turns: 8", y)

    def test_criteria_without_the_brief_ids_go_back_to_claude(self):
        fake = FakeBriefClaude(bare=[3], cite_unknown=[4])
        sp = bspec()
        p = gen_core.plan(sp, fake)
        out = gen_core.write(sp, p["rows"], p["personas"], fake)
        self.assertEqual((sorted(out["fixed"]), out["dropped"]), ([3, 4], []))
        self.assertEqual(sum(1 for c in fake.calls if c.startswith("the arena's validator")), 1)
        self.assertTrue(gen_core.check(out["yaml"])["ok"])

    def test_a_criterion_must_be_a_sentence_with_only_ids_in_brackets(self):
        sp = bspec()
        row = {"slot": 3, "mode": "scripted", "lang": "en", "scenario": "S2", "user": "P2", "persona": "p2-en"}
        case = {"script": ["Help me eat less bread", {"text": "That won't work", "expect": "B1"}],
                "expect": ["[doubt-handling][B1] Rudi asks why before the next tip."]}
        msgs = " | ".join(gen_core.brief_checks(case, row, sp))
        self.assertIn("criterion “B1” is not a sentence", msgs)
        self.assertIn("starts with [doubt-handling], which is not an ID of the brief", msgs)
        good = {"script": ["Help me eat less bread", {"text": "That won't work", "expect": "[B1] Rudi asks what makes them doubt it."}],
                "expect": ["[B1][N1] Rudi asks why before the next tip."]}
        self.assertEqual(gen_core.brief_checks(good, row, sp), [])

    def test_a_plan_from_another_brief_or_from_plain_words_is_refused(self):
        fake = FakeBriefClaude()
        with self.assertRaises(gen_core.GenError):
            gen_core.write(bspec(), [{"slot": 1, "mode": "intent", "lang": "en", "persona": "mark", "title": "x", "probe": "y"}], {}, fake)

    def test_redo_keeps_the_scenario_and_the_user(self):
        fake = FakeBriefClaude()
        sp = bspec()
        p = gen_core.plan(sp, fake)
        got = gen_core.redo(sp, p["rows"], p["personas"], 2, fake)
        self.assertEqual((got["row"]["scenario"], got["row"]["user"], got["row"]["persona"]), ("S2", "P2", "p2-en"))

    def test_the_gym_01_brief_generates_a_valid_batch(self):
        with open(GYM01, encoding="utf-8") as f:
            sp = bspec(f.read())
        fake = FakeBriefClaude()
        p = gen_core.plan(sp, fake)
        out = gen_core.write(sp, p["rows"], p["personas"], fake)
        rep = gen_core.check(out["yaml"])
        self.assertTrue(rep["ok"], rep["issues"][:5])
        self.assertEqual((rep["cases"], out["dropped"], out["file"]), (30, [], "gym-01-first-contact__input.yaml"))


class BriefRouteTests(unittest.TestCase):
    def test_the_console_can_check_a_brief(self):
        class C:
            def ask(self, prompt, model):
                raise AssertionError("no Claude for /brief")

            def kill(self):
                pass
        tmp = tempfile.mkdtemp()
        app = generator.App(C(), generator.PORT, generator.ORIGINS, os.path.join(tmp, "r"), os.path.join(tmp, "h"))
        tok = app.route("POST", "/pair", {"code": app.code}, None)[1]["token"]
        code, out = app.route("POST", "/brief", {"text": SMALL}, tok)
        self.assertEqual((code, out["brief"]["id"], out["brief"]["scenarios"][0]["cases"]), (200, "test-brief", 2))
        code, out = app.route("POST", "/brief", {"text": brief(**{"users: P2, P1": "users: P9"})}, tok)
        self.assertEqual((code, out["error"]), (400, "bad_brief"))
        self.assertTrue(any(i["line"] > 0 and "P9" in i["message"] for i in out["issues"]))
        self.assertEqual(app.route("GET", "/health", {}, tok)[1]["version"], "2")


if __name__ == "__main__":
    unittest.main()
