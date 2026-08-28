# Tester Console — Blueprint & Status

A closed, login-gated console that lets invited third-party testers experience Rudi through all
three channels and leave feedback on each. Static pages on GitHub Pages; every decision made by
one Lambda. Companion to [whatsapp-operator-console.md](whatsapp-operator-console.md).

**Not to be confused with** `meetrudi-test-console` — that is the *internal* personality harness
with one fixed login. This is the *external* cohort console with per-tester accounts.

## Decisions (locked in review)

1. **Hosted on GitHub Pages**, admin pane included. Consequence: the API owns all auth, CORS is
   pinned to the Pages origin, and no PII may ever sit in the static bundle.
2. **Four call goals, system-set**: `GET_TO_KNOW`, `SET_NEARTERM_GOAL`, `GOAL_FOLLOWUP`,
   `REINSTATE_TALK`. Assigned round-robin at registration, changeable only by an admin, and
   **never shown to the tester** — `Tester.public()` deliberately omits the field.
3. **Five calls per tester**, and only a **connected** call is deducted. Voicemail, no answer and
   failures cost nothing.
4. **Rudi never talks to voicemail.** Twilio AMD is on; `meetrudi-call-status` hangs up on any
   machine verdict. No automatic retry — calls are human-triggered, so the tester just asks again.
5. **One call at a time**, queued. Tester-facing copy blames demand, not our capacity.
6. **Quiet hours 21:30–06:30 Europe/Brussels** are honoured in the console, not just the
   dispatcher: the button is disabled and says why.
7. **Two cohorts, `nl-BE` and `en`.** Locale is chosen on the form, stored on the profile, and
   drives the console, the mails, Rudi's replies and his voice.
8. **Registration is private and closable.** The link is shared with invitees only; an admin
   closes the form once the cohort is full.
9. **Desktop only.** Testers are told so on the form; no mobile layout work.
10. **Erasure is phase 2.** The admin button is drawn but not wired, and the runner role
    deliberately cannot delete tester profiles.

## The three tracks

| Track | What the tester does | Engine |
|---|---|---|
| A — chat | Types to Rudi in the console | `responder.respond` → `gateway.generate`, isolated under `tester-conversations/` |
| B — call | Presses "Call me now"; Rudi rings the registered number | `meetrudi-call-dispatcher` via its Function URL |
| C — WhatsApp | Sends the Twilio join phrase from their own phone | the live WhatsApp path |

Tracks A and C are **separate conversation threads** on a shared profile and goal — A is keyed by
`testerId`, C by the phone pseudonym, and merging them would mean merging two threads. One Rudi,
one goal, two histories.

## Components

| Component | Type | Role |
|---|---|---|
| `meetrudi-tester-api` | Lambda (Function URL) | Registration, auth, sessions, all three tracks, feedback, admin |
| `meetrudi-call-status` | Lambda (Function URL) | Twilio status callbacks; hangs up on voicemail, writes the terminal outcome |
| `TesterStore` (`tester_store.py`) | Library | S3 system-of-record for testers, sessions, links, queue, settings |
| `tester_mail.py` | Library | SES sender, NL-BE/EN verification + reset mail |
| `site/tester-console/` | Static app | Six pages, shipped to GitHub Pages |

## Storage

```
testers/{testerId}/profile.json                 # PII, credentials, call ledger, track progress
testers/{testerId}/feedback/{track}.json        # latest answer + full history
tester-console/sessions/{sha256(token)}.json    # live sessions, idle-expiring
tester-console/tokens/{sha256(token)}.json      # single-use verify / reset links
tester-console/settings.json                    # registration open, calling paused, daily cap
tester-console/call-queue.json                  # who is on the line, who is waiting
tester-console/call-day/{YYYY-MM-DD}.json       # cohort-wide daily call counter
tester-conversations/{testerId}/...             # Track A thread (ConversationStore layout)
```

`testerId` = `HMAC(salt, lowercased email)`, so a resubmitted registration updates one record
instead of forking a second, and no key anywhere contains a name, an address or a number (§5).
Tokens are stored as SHA-256 digests: a bucket listing yields no usable credential, and neither
does an object read.

## Security posture

- **Passwords**: PBKDF2-HMAC-SHA256, 600k rounds, per-tester random salt. Policy `8+ chars,
  lower, upper, digit` enforced server-side by regex; the browser only previews it.
- **Sessions**: opaque token, 10-minute idle expiry checked **on every request**. A browser that
  keeps its token past the timeout gets nothing.
- **Links**: single-use, 24-hour TTL, burnt by deletion so a replay finds nothing.
- **Lockout**: 10 failed logins locks the account; only an admin unlocks it.
- **No enumeration**: an unknown email and a wrong password answer identically, and `/forgot`
  always answers 200.
- **Admin**: a password in Secrets Manager mints a session with `role=admin`. Every `/admin/*`
  route re-checks the role. Fails **closed** — 503 until the secret exists.
- **The number is hard-locked**: no route in the API dials anything but the number captured at
  registration.
- **Phone is masked even in the admin payload** (`+32 479 ••• •56`) — nobody needs the full
  number to run a cohort, and that payload crosses the network to a browser.

## Tests

Zero-dependency, no boto3, no network, synthetic data only:

```
python -m unittest discover -s services/whatsapp/tests -v      # 244 tests
python -m unittest discover -s services/call/tests -v          # 105 tests
```

93 of those are new: `test_tester_store.py` (identity, single-use links, idle sessions, queue,
ledger), `test_tester_api.py` (validation, auth, session gating, call gates, the
connected-only deduction rule, admin, routing), `test_status.py` (every AMD verdict, terminal
statuses, and that the dispatcher really asks Twilio for machine detection).

## Deploy

### One-time setup (console only — see [infra/iam/README.md §6](../infra/iam/README.md))

1. Add both statements from `infra/iam/meetrudi-lambda-runner.tester-console-add.json` to the
   `meetrudi-lambda-runner` inline policy.
2. Create the secret `meetrudi/tester-console/admin` from
   `infra/iam/meetrudi-tester-console-admin.example.json`.
3. Verify an SES sender identity in **eu-central-1** (and request production access — the sandbox
   only sends to verified addresses).
4. Copy `deploy.local.example.json` to `deploy.local.json` and fill in the origin, the sender, the
   support address, the WhatsApp number and the join phrase. That file is git-ignored: phone
   numbers and addresses do not belong in source.

### Deploy

The call stack first — the tester API reads its dispatch URL straight off that stack's output:

```cmd
python deploy.py call
python deploy.py whatsapp
```

Then take the `Tester API` URL printed at the end, put it into
`site/tester-console/config.js` as `API_BASE`, and push — GitHub Pages serves the console.

### Verify

```cmd
curl https://<tester-api-url>/health
```

Expect `{"ok": true, "service": "meetrudi-tester-api"}`. Then open `admin.html`, sign in with the
secret's password, and confirm the roster loads and registration shows as open.

## Status

| Block | Scope | Status |
|---|---|---|
| T0 — Store | `TesterStore`, identity, links, sessions, queue, ledger | ✅ done |
| T1 — Registration | validation, S3 profile, goal + help areas, honeypot | ✅ done |
| T2 — Auth | verification mail, password set, login, lockout, reset | ✅ done |
| T3 — Track A | console chat on the real engine, isolated prefix | ✅ done |
| T4 — Track B | call gates, queue, dispatch, connected-only ledger | ✅ done |
| T5 — Track C | join-phrase instructions, self-reported start | ✅ done |
| T6 — Feedback | 0–10 scale + free text, per-track send, history kept | ✅ done |
| T7 — Admin | KPIs, roster, all roster actions, settings, CSV export | ✅ done |
| T8 — Voicemail | AMD on the dial + status callback + polite hang-up | ✅ code done, needs a real call to confirm |
| T9 — Erasure | cascade across every store | ⏳ phase 2, deliberately |

**Before the first real tester:** SES production access, and one live call placed to a phone that
goes to voicemail — T8 is the only block whose behaviour cannot be proven without Twilio.
