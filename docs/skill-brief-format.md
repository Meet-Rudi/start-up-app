# Skill brief — `rudi-skill-brief@1`

A **skill brief** defines one skill that Rudi should be trained on, in a form both people and the
Training Arena's Generator read. A person writes it; the Generator turns it into a batch of arena
cases whose every criterion cites the brief; the arena runs them against the deployed Rudi; the
Improvement Extractor reads the results. Because each requirement has an **ID**, a finding can be
traced all the way back: "N5 broken in 6 of 30 cases" points at one line of the brief.

- **Where:** `evals/skill-briefs/<id>.skill.md`. Example: [gym-01-first-contact](../evals/skill-briefs/gym-01-first-contact.skill.md).
- **Blank template:** [site/training-arena/skill-brief-template.md](../site/training-arena/skill-brief-template.md)
  (the console's Generator offers it as a download).
- **Use:** console → Generator → *Skill brief* → load the file. The generator checks it and shows its
  problems by line; the plan comes from its scenarios; the cases cite its IDs.
- **Code:** `services/training-arena/generator/skill_brief.py` (reading and checking),
  `gen_core.py` (planning and writing). Tests: `services/training-arena/tests/test_skill_brief.py`.

## 1. Shape of the file

A Markdown file: YAML settings between two `---` lines, then the sections below as `##` headings
(numbers optional), in any order. Requirements are bullets that start with a bold ID:

```markdown
- **B3** (each reply) Asks at most one question per reply.
- **S2** The user doubts a tip.
  - users: P6
  - mode: goal
  - checks: B8, N4
  - pass when: Rudi first asks what makes them doubt it.
```

- `- **<ID>**` starts an item; an optional `(tag)` follows the ID; then its text.
- Lines indented under the item continue its text.
- Indented sub-bullets `- name: value` are its **fields**; indented lines under a field continue it.
- Anything else in a section is prose (kept, shown to Claude, not checked).
- An ID is a letter and a number. The letter belongs to the section; an ID is never reused for
  something else, even after the item is deleted — results of earlier batches cite it.

## 2. Settings

| Setting | Required | Meaning |
|---|---|---|
| `format` | yes | `rudi-skill-brief@1` |
| `id` | yes | Lower-case letters, digits, hyphens; stable — batches and reports cite it. |
| `title` | yes | A short name. |
| `version` | yes | Bump on every change; the batch's header and tags record it. |
| `status` | no | `draft` or `approved`. |
| `owner` | no | A role, not a person's name. |
| `catalogue_skills` | no | Skill ids from `services/training-arena/seed/skills/rudi-skills.yaml` this brief exercises; unknown ids are refused. |
| `channel` | no | `chat` (the only channel the arena runs). |
| `languages` | no | Shares of the cases, adding up to 100: `{en: 100}`, `{en: 70, nl: 30}`. Default all English. |
| `batch.name` | no | The batch's name in the arena; default the title. |
| `batch.cases` | no | Number of cases, 1–100 (default 10). At least one per scenario. |
| `batch.max_turns` | no | User messages per case at most, 2–40 (default 12). |
| `batch.max_attempts` | no | Goal cases: pushes before the simulated user gives up, 0–10 (default 3). |
| `batch.case_budget_eur` / `batch.batch_budget_eur` | no | Budgets, as in the arena (defaults €0.05 per case; cases × that, at least €1, for the batch). |
| `release_gate` | no | When the skill counts as trained, e.g. "every scenario passes in at least 4 of 5 runs". Copied into the batch. |

## 3. Sections

| Section | IDs | Required | What goes in | What the Generator does with it |
|---|---|---|---|---|
| Purpose | — | yes | Why the skill matters, at most five lines. | Context for Claude; the batch description. |
| Scope | — | no | In and out: moment, channel, languages. | Context. |
| Outcome | O | yes | The user-side result, as a reader of the transcript would see it. | Criteria may cite O items. |
| Must do | B | yes | One observable behaviour per item. Tag: `(each reply)`, `(whole conversation)` or `(at the end)`. | Criteria. |
| Must never | N | yes | One observable failure per item. Tag: `(violation)` — stops the case — or `(defect)`. | `abort_when` (violations) and criteria. |
| When rules collide | R | no | Which rule wins, and when. | Shapes how criteria are worded. |
| Boundaries | L | no | Allowed / not allowed / what Rudi does at the line; field `examples:` on each side. | Overrides the Generator's default medical caution. |
| Users | P | yes | User types. Fields: `first message`, `wants` (required), `resists`, `reveals when asked`, `won over when` (required), `style`. | Personas, objectives, openings, reveals. |
| Scenarios | S | yes | Concrete situations. Fields: `users` (P ids, required), `mode` (`scripted`, `goal`, `hybrid`; required), `setup`, `checks` (B/N/R/L/O/K ids, required), `pass when` (required), `fail when`, `cases`. | The plan: one or more cases per scenario. |
| Examples | E | no | Good and bad replies. Fields: `user`, `good`, `bad`, `why`. | Calibrates Claude's criteria. |
| Known limits | K | no | Engine behaviour the batch will run into today, and the catalogue skill it belongs to. | Criteria account for it; findings read correctly. |

## 4. From brief to batch

1. **Slots.** Each scenario gets its `cases` (scenarios without one share the rest of `batch.cases`
   evenly). Within a scenario its users take turns. Languages are spread over all cases in the
   `languages` shares. The mode is the scenario's.
2. **Plan.** Claude titles every case with its own angle and writes one persona per user type and
   language. Nothing else is left to Claude: the cases' scenarios, users, modes and languages are the
   brief's.
3. **Write.** Claude writes each case from its scenario and user type:
   - scripted: the user's messages, a check on Rudi's reply to a named moment (`expect` on that
     message) or a time jump (`gap`) where the scenario asks for one, and 2–6 criteria judged at the end;
   - goal: objective, opening, reveals, `success_when` (the scenario's pass condition, judged after
     every reply), `abort_when` (its violations), and 1–5 criteria judged at the end;
   - hybrid: one or two scripted opening messages, then the goal fields.

   **Every criterion starts with the IDs it checks**, e.g. `[B3][N2] In every reply Rudi asks at most
   one question.` A case whose criteria cite none of its scenario's checks, or cite an ID the brief
   does not have, goes back to Claude to fix (twice at most) and is otherwise left out.
4. **Check.** The arena's own validator checks the file, as for every generated batch.

In the arena, a goal case that meets `success_when` but fails one of its end criteria is **failed**,
not a success. The verdict names the first failed criterion.

## 5. What the generator refuses

- Missing settings (`format`, `id`, `title`, `version`), a `channel` other than chat, language shares
  that do not add up to 100, numbers out of range, a case budget above the batch budget.
- A required section missing or without items; an ID in the wrong section or used twice; a B or N
  tag that is not one of the above; a user without `wants` or `won over when`; a scenario without
  `users`, `mode`, `checks` or `pass when`.
- A scenario naming a user or check the brief does not have; fewer `batch.cases` than the scenarios
  need.
- A `catalogue_skills` id that is not in the skills catalogue.
- Anything that looks like a phone number, e-mail address or IBAN. Briefs are synthetic: a real
  conversation (for instance a pilot partner's transcript) is rewritten as a scenario, never pasted in.

Fields the generator does not read are warnings, not errors.

## 6. Writing a good brief

- **One behaviour per item**, observable in Rudi's replies alone: "asks at most one question per
  reply", not "is curious". Feelings go in Outcome, with how a reader would see them.
- **Numbers where you can**: sentences, questions, turns.
- **Say which rule wins** when two can collide (R items); otherwise the judge decides case by case.
- **Put examples on both sides of a boundary.** "General nutrition is fine" means little without
  "what can I eat for breakfast" next to "can I skip my metformin".
- **Give each user type its own success** (`won over when`): a user who only wants a recipe passes
  differently from one who needs to be heard.
- **Turn feedback into scenarios** with an exact setup: "at the third message the user switches to
  sleep" can be tested; "Rudi doesn't follow the conversation" cannot.
- **Name the known limits.** When today's engine is bound to fail a rule, say so and which catalogue
  skill it belongs to, so the results read as findings rather than noise.
- Criteria in **English** (the judge's language); user and Rudi lines in the case's language.
- Process notes and decisions do not belong in a brief (they go in project.md).
