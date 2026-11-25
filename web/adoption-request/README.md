# Adoption request form

The page where an organisation asks for its own Rudi (decision #48). It replaces the single-page
mock-up `web/mockups/clone-request.html` with a form in steps that saves after every step.

| File | What it is |
|---|---|
| `rudi-adoption-request.html` | **The page to put on the website.** One self-contained file: styles, fonts, logo and mascot inlined, the endpoint filled in. No login, no link to this repository, no third-party request. |
| `adoption-request.src.html` | Its source, linking `web/brand/`. Edit this one. |
| `build.py` | Builds the first from the second. `python deploy.py prospect-intake` runs it after each deploy. |
| [`services/prospect-intake/`](../../services/prospect-intake/) | The endpoint, `meetrudi-internal-prod-prospect-intake`, and the questions (`src/questionnaire.json`). |

## How it works

1. The page loads, asks the endpoint for the first step (contact details) and shows it. **The page
   holds no questions of its own**: the endpoint hands each step out only after every step before
   it is saved, so viewing the page's source shows the contact step and nothing more.
2. **Contact details come first.** Saving them creates the request (`prs_…` id) and stores it at
   once, so a person who stops after step 1 has still left us a way to follow up. The browser
   keeps only the id and a token, never the answers.
3. Each "Save and continue" stores that step. "Finish later" and "Back" store what is filled in
   without checking it. Coming back on the same device picks up where the person left off.
4. A progress bar and a step list show how far along the person is; saved steps can be revisited.
5. Sending the last step moves the request from `partial/` to `complete/`.

Steps (questionnaire `rudi-adoption-request@1`, form version `2026-11-24.2`): About you · Your
programme · Where people reach Rudi · What Rudi does · Rudi's personality · **Staying in control**
(how involved the team wants to be, stepping in, approval, alerts, coverage) · **What you see**
(cockpit figures, reports, team) · Set-up, and send (material, a closed **"Bringing your own AI or
tools?"** group where Rudi-supplied AI is the default, agreements).

## Where requests are stored

EU data bucket `meetrudi-ai-data-949753869755`, outside every tenant's folder (conventions §2):

```
platform/prospects/partial/prs_<id>.partial.json     started, not sent  ← the follow-up list
platform/prospects/complete/prs_<id>.complete.json   sent
```

Both the folder and the file name say the state, and each record repeats it in `"state"`.

### Rules for an automatic follow-up

- **Candidates:** list `platform/prospects/partial/`. Skip a record whose `state` is `complete`
  or whose id also exists under `complete/` (the endpoint cannot delete a partial until
  USER-TODO #5 is done, so it overwrites it as complete instead).
- **Contact only when `follow_up.may_contact` is true.** The person agreed to it on step 1
  ("I agree to be contacted by the Rudi team if any crucial input is missing before my Rudi
  clone goes live"); the agreement's text and time are in `agreements.contact_agreement`. That
  the answers are saved after each step is said in step 1's introduction, not asked.
- **What to read:** `follow_up` holds the contact details, `stopped_before` /
  `stopped_before_title` (the first unfinished step) and `last_activity_at`; `progress.percent`
  says how far they got; `events` lists every save with its time.
- **Marketing is separate:** only `follow_up.wants_updates` (the optional tick on the last step)
  allows news about Rudi for organisations.
- The same person on another device starts a new request; de-duplicate by e-mail.

### Record (synthetic example, abridged)

```json
{
  "record_schema": "rudi-prospect@1",
  "id": "prs_91641tmt9dy8gg9fkq8sptc55tnt1155",
  "state": "partial",
  "questionnaire": "rudi-adoption-request@1",
  "form_version": "2026-11-24",
  "lang": "en",
  "started_at": "2026-11-24T19:48:16+00:00",
  "updated_at": "2026-11-24T19:49:02+00:00",
  "completed_at": null,
  "progress": {"done": ["contact"], "total": 8, "percent": 12, "next_step": "programme"},
  "follow_up": {"organisation": "Example Health Lab", "contact_name": "Alex Example",
                "contact_role": "Programme lead", "contact_email": "alex@example.org",
                "contact_phone": null, "country": "be", "may_contact": true,
                "wants_updates": false, "stopped_before": "programme",
                "stopped_before_title": "Your programme",
                "last_activity_at": "2026-11-24T19:49:02+00:00"},
  "answers": {"contact": {"…": "…"}, "programme": {"purpose": "…"}},
  "agreements": {"contact_agreement": {"granted": true, "text": "I agree to be contacted…", "at": "…"}},
  "events": [{"at": "…", "kind": "start", "step": "contact"},
             {"at": "…", "kind": "draft", "step": "programme"}],
  "token_sha256": "…"
}
```

## E-mail notifications

The endpoint tells the team by e-mail (SES, eu-central-1; `services/prospect-intake/src/notify.py`),
To `prospect_notify_to` with `prospect_notify_cc` in copy, from `prospect_mail_from`:

| When | Subject | Body |
|---|---|---|
| A request's first step is saved (contact details and the contact agreement) | `New prospect started configuration - <name> \| <e-mail>` | The first step's answers, the request id and where it is stored |
| The request is sent | `New Rudi clone REQUEST COMPLETE - <name> \| <e-mail>` | Every step's answers in one table (option labels, "—" for an open optional question), the request id, times and where it is stored |

- Bots (honeypot, too fast), drafts and the steps in between send nothing.
- Sent after the record is stored, and best-effort: a refused or slow mail is logged
  (`prospect.notify_failed`) and never fails the person's save. The S3 record is the reference.
- The addresses live in `deploy.local.json` (git-ignored), never in source. The sender must be
  the SES identity `meetrudi-lambda-runner` may send as (`support@meetrudi.eu`, its
  `ses:FromAddress` condition); another sender is refused by SES.
- These mails carry the prospect's name, contact details and answers to our own mailboxes, by
  design (decision #48); keep that mailbox provider EU-hosted under a DPA (CLAUDE.md §0.3).

## Putting it on the website

- Upload `rudi-adoption-request.html` as it is, at any URL (e.g. `/en/request`). It carries its
  own header and footer, linking to meetrudi.eu pages; trim them if the site wraps the page in
  its own.
- It sets a Content-Security-Policy that allows only its own inline code and the endpoint. If the
  site injects its own scripts into the page, they are blocked: that is deliberate (no
  third-party requests, CLAUDE.md §1), so drop the `<meta http-equiv="Content-Security-Policy">`
  only after checking what would load.
- **Lock the endpoint to the site:** set `"prospect_form_origin": "https://meetrudi.eu"` (the
  site's exact origin) in `deploy.local.json` and run `python deploy.py prospect-intake`. Until
  then the endpoint accepts calls from any page, which is what testing from a local file needs.

## Changing the questions

Edit `services/prospect-intake/src/questionnaire.json`, bump `form_version`, run the tests and
`python deploy.py prospect-intake`. The page needs no change. Field types: `text`, `email`,
`tel`, `url`, `textarea`, `select`, `radio`, `checkbox`, `scale`, `time_window`, `agree`,
`check`, `note`, `group`. A field or a step may carry `show_if` on fields asked before it
(`is`, `has`, `in`, `any`, `all`), which is how the questionnaire branches, for instance by
organisation kind. `questionnaire.check()` refuses a definition the browser or the server could
misread. Tests: `python -m unittest discover -s services/prospect-intake/tests -v`.

## Abuse guards

The endpoint is public. It caps the body at 32 KB, drops submissions with the honeypot filled
or a first step completed in under 1.5 s (answering as if they succeeded), validates every answer
against the questionnaire, and refuses any step beyond the saved ones or a request without its
token. Since every started request mails the team, a script posting fake starts would also fill
that inbox. Not yet: rate limiting or a CAPTCHA (add Cloudflare Turnstile or AWS WAF if spam
appears), and a reserved-concurrency cap, which needs the Lambda quota raise (USER-TODO #1)
first. Until then a flood on this endpoint shares the account's 10 concurrent runs with CM's
live services, as the consent intake does today.
