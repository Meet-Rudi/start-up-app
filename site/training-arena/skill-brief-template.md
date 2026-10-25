---
format: rudi-skill-brief@1
id: my-skill                     # lower-case letters, digits and hyphens; never changes
title: My skill
version: 1                       # bump on every change; the batch records it
status: draft                    # draft | approved
owner: product
catalogue_skills: [goal-setting] # ids from services/training-arena/seed/skills/rudi-skills.yaml
channel: chat                    # the arena runs chat only
languages: {en: 100}             # shares of the cases, e.g. {en: 70, nl: 30}
batch:
  name: My skill
  cases: 2                       # at least one per scenario
  max_turns: 10                  # user messages per case at most (2–40)
  max_attempts: 3                # goal cases: pushes before the user gives up (0 = no limit)
  case_budget_eur: 0.05
  batch_budget_eur: 1.00
release_gate: Every scenario passes in at least 4 of 5 runs.
---

# My skill

<!-- Format: docs/skill-brief-format.md. One requirement per bullet, each with its own ID.
     Write criteria in English and observable in Rudi's replies alone. Synthetic only:
     no real people, transcripts, names, numbers or addresses. -->

## 1. Purpose

Why this skill matters, in at most five lines.

## 2. Scope

- In: which moment of the relationship, channel and languages.
- Out: what this brief does not test.

## 3. Outcome (O)

- **O1** The result from the user's side, as a reader of the transcript would see it.

## 4. Must do (B)

<!-- Tag each with (each reply), (whole conversation) or (at the end). -->

- **B1** (each reply) One observable behaviour, e.g. "asks at most one question per reply".

## 5. Must never (N)

<!-- Tag each with (violation) — stops the case — or (defect). -->

- **N1** (defect) One observable failure.

## 6. When rules collide (R)

- **R1** Which rule wins, and when.

## 7. Boundaries (L)

- **L1** Allowed: …
  - examples: one example on this side of the line
- **L2** Not allowed: …
  - examples: one example on the other side

## 8. Users (P)

- **P1** Who they are, in one line.
  - first message: "What they write first."
  - wants: what they want from Rudi
  - resists: how they push back
  - reveals when asked: facts they share only when asked
  - won over when: what success looks like for this user

## 9. Scenarios (S)

<!-- mode: scripted (fixed messages), goal (a simulated user improvises) or hybrid (both).
     checks: the B, N, R, L, O or K items this scenario tests. cases: optional. -->

- **S1** A concrete situation, in one line.
  - users: P1
  - mode: goal
  - setup: the exact moment or messages that set it up
  - checks: B1, N1
  - pass when: what Rudi's replies must show
  - fail when: an outright violation that stops the case (optional)
- **S2** Another situation.
  - users: P1
  - mode: scripted
  - setup: "First message", then "second message"
  - checks: B1
  - pass when: what Rudi's replies must show

## 10. Examples (E)

- **E1** A moment worth showing.
  - user: "What the user says."
  - good: "A reply that meets the brief."
  - bad: "A reply that does not."
  - why: what makes the difference

## 11. Known limits (K)

- **K1** Engine behaviour this brief will run into today, and which catalogue skill it belongs to.
