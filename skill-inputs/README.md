# Skill inputs — drafts per pilot use case

Draft inputs for training Rudi on a new skill, one subfolder per use case, so each tenant's material
stays separate. A draft becomes a real brief once the partner has answered and we approve it; it then
moves to `evals/skill-briefs/` and the Training Arena's Generator builds batches from it.

| Folder | Use case | Skill | Status |
|---|---|---|---|
| [uc-01-smart-running](uc-01-smart-running/) | Smart Running: runner check-in pilot (10–20 runners, WhatsApp) | TA — Training adherence | Brief v3, questionnaire v2; waiting for the partner's answers. Send `rudi-smart-running-pilot.html`. |

## What each subfolder holds

- `<id>.skill.md`: the skill brief in the machine format `rudi-skill-brief@1`: YAML settings at the
  top, then the 11 sections (Purpose, Scope, Outcome, Must do, Must never, When rules collide,
  Boundaries, Users, Scenarios, Examples, Known limits). The Generator loads this file as is.
  Format: [docs/skill-brief-format.md](../docs/skill-brief-format.md).
- `<id>.nl.yaml`: the Dutch reading translation of the brief, keyed by item ID. The Generator never
  reads it (criteria stay English). It records a fingerprint of the English file, so the review copy
  flags it as out of date when the brief changes.
- `questions-for-<partner>.yaml`: the questionnaire, English and Dutch. Each question feeds one brief
  section, shows our first answer as an example, and lists the draft items it confirms. Question IDs
  (`q1`, `q2`, …) are stable: the form stores answers under them.
- `rudi-<partner>-pilot.html`: **the one file to send.** A self-contained page rendered from the three
  sources above, holding both documents: the questionnaire (shown when the file is opened) and the
  review copy of the brief. Every link between them opens a new tab of the same file showing the
  other document at the right item, so it works wherever the file is saved, with nothing else beside
  it. Both documents have an EN | NL switch. The questionnaire keeps answers in the browser as the
  partner types (they survive leaving the page, reloading, and other tabs of the file; nothing is sent
  anywhere). The blue **SAVE draft** button writes a .txt file with every question and answer in the
  selected language; once every question is answered it reads **SAVE final version**. Edit the
  sources, never this file.
- `rudi-<partner>-questionnaire.html`: the questionnaire alone, for a partner who should not get the
  brief: no brief, no links to it, no internal notes. Same EN | NL switch, autosave and SAVE button,
  and the same answer storage, so answers typed in either file show up in the other in the same
  browser.

## Rules

- Synthetic only. No real users, transcripts, names, phone numbers or e-mail addresses (GDPR). The
  Generator refuses a brief that contains them.
- An item ID (B3, N5, S2, …) is never reused for something else, even after the item is deleted.
- Process notes and decisions go in `project.md`, not in a brief.
