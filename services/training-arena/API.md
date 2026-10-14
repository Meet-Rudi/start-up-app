# meetrudi-arena-api — contract

Base URL: the `ApiUrl` output of the `meetrudi-training-arena` stack (a Lambda Function URL), set as
`API_BASE` in `site/training-arena/config.js`. CORS is pinned to the GitHub Pages origin.

All requests and responses are JSON. After sign-in, send the session token in the header
**`X-Arena-Token`**. Errors are `{"error": "<code>", "detail": "<human sentence>"}` with an HTTP
status: 400 bad input, 401 not signed in / session ended, 403 forbidden (or `must_change_password`),
404 not found, 409 conflict, 503 `auth_not_configured`.

Times shown to people are pre-formatted on the Brussels clock (`"2026-10-13 10:14"`).

## Sign-in and account

| Route | Body | Response |
|---|---|---|
| `POST /login` | `{username, password}` | `{token, user: {u, role, status, must_change}}` — 401 `invalid_credentials`, 403 `account_locked`, 503 `auth_not_configured` |
| `POST /logout` | — | `{ok}` |
| `GET /me` | — | `{user, spent_today, budgets: {user_day_budget, month_budget, case_default, batch_default, case_ceiling, batch_ceiling}, settings: {max_parallel, launch_rate, think_time, max_turns_cap}, bucket}` |
| `POST /account/password` | `{current, new}` | `{ok}` — 400 `weak_password` (12+, lower, upper, digit), 401 wrong current |

While `user.must_change` is true, every route except `/me`, `/account/password` and `/logout`
answers 403 `must_change_password`.

## Dashboard

`GET /dashboard?days=30` →

```
{ daily: [ { date, success, failed, violation, error, none, total, p50, p95,   // ms, from a histogram
             models: {name: replies}, errors: {code: n},
             modes: {scripted|intent|hybrid: {n, eur, max, over}},
             roles: {rudi, tester, judge, infra},                            // euro; infra = AWS estimate
             cost: {model name or "AWS (estimate)": eur}, infra } … oldest first ],
  batches: [ <batch view> … active first, then the 20 most recent ],
  failingKeys: [ {key, runs, bad, last, when, kinds} … ],
  expensive: [ {id, key, turns, cost, cap, outcome, stop} … ],   // the period's 6 dearest cases
  users: [ {u, spentToday} ],          // every spender for a super-admin, only yourself for a tester
  budgets: {month_budget, user_day_budget}, now }
```

## Batch view (used everywhere a batch appears)

```
{ id, name, by, status, submitted, started, finished, total,
  c: {success, failed, violation, error, completed, cancelled, over_budget, running, queued},
  eta, parts, modes: {mode: n}, locales: {loc: n}, tags, desc, spent, cap, caseCap,
  roles: {rudi, tester, judge, infra}, files: [output file names], hold, quiet_only, free_slow, stem,
  input_file, validation: {ok, errors, warnings, cases, cost_eur, duration_s}, rudi_release, snapshots }
```

`status`: `draft` → `validating` → `invalid` | `ready` → `launching` → `queued` → `running` ⇄
`paused` → `assembling` → `completed` | `completed_with_errors` | `budget_exhausted` | `cancelled`.

## Submitting and running a batch

1. `POST /uploads {filename, size}` → `{url, key}`. `PUT` the file's bytes to `url` (no extra
   headers). Or skip this and use an object already in `inbox/`.
2. `POST /batches {key | s3_uri, name, quiet_only}` → 201 `{batch}` with status `draft`;
   validation starts by itself.
3. Poll `GET /batches/{id}` → `{batch, validation}` until `batch.status` is `ready` or `invalid`.
   `validation` (null until done):
   ```
   { ok, errors, warnings, issues: [[ "e"|"w", line, message ] …], cases,
     by_mode, by_locale, by_persona, turns, rudi_replies, llm_calls, cost_eur, duration_s,
     budget: {batch, batch_src, case, case_src}, batch: {name, description, tags}, checked_at }
   ```
4. `POST /batches/{id}/launch {synthetic: true, quiet_only}` → `{ok}` (400 `confirm_synthetic`
   without the flag; 409 `not_ready`).

Other actions (submitter or super-admin): `POST /batches/{id}/validate`, `/pause`, `/resume`,
`/cancel`, `/rerun {only: "not_ok" | "all"}` → `{batch}` (a new batch), `/snapshot` (partial
results of a running batch), `/quiet {quiet_only}` — hold a batch for quiet hours (21:30–06:30,
Europe/Brussels) or, with `false`, let a held one start now. A held batch's `eta` says so.
`/free_slow {on}` — free slow mode: every model call waits for its free tier (one case at a time,
about one Rudi reply a minute); `false` speeds the batch up again. Also accepted as `free_slow` in
`POST /batches` and `/launch`; the batch view carries `free_slow`, and its `eta` starts with
"free slow — " (or is the hold "free slow: today's free allowance is used up — …").

`GET /batches` → `{batches: [batch view …]}` (most recent first).

`GET /batches/{id}/estimate` → the launch estimate, as if launched now (409 `not_ready` before the
check):

```
{ now: "2026-10-14 17:47",
  plans: [ {free_slow, quiet, eur, by_role: {rudi, tester, judge, infra}, start, end,   // "yyyy-mm-dd hh:mm", Europe/Brussels
            duration_s, work_s, parallel, free_share: {rudi, tester, judge}} … ],     // normal / free slow × now / quiet hours
  assumptions: [text …], counts: {cases, rudi_replies, tester_calls, judge_calls} }   // worst case
```

`GET /batches/{id}/cases?offset=0&limit=2000` → `{total, rows: [{seq, id, key, title, mode, loc,
persona, outcome, stop, turns, dur, errs, cost, cap, roles, tags}]}` — `outcome` includes `queued`
and `running` for cases not finished yet.

`GET /batches/{id}/files/{name}` → `{url}` — a 15-minute signed download link for the input file
(`batch.input_file`) or any name in `batch.files` / `batch.snapshots[].files`.

## Cases

`GET /cases/{case_id}` → `{case}` in the case viewer's shape:

```
{ id, n, key, title, mode, loc, persona, outcome, stop, turns, dur, errs, verdict,
  checks: [[result, text, why] …], final: {phase, goal, commitment, …},
  errors: [[turn, code, detail] …], cost, cap, roles: {rudi, tester, judge, infra},
  tx: [ {t, f: "u"|"r", at: "HH:MM:SS", x: text, m: {source, pushing, gap, eur, note,
          ms, model, phase, lang, guard, err, judge, ok, bad}}
        | {gap: "22h later on Rudi's clock"} | {unanswered: "…"} … ] | null,   // null while not finished
  batch: <batch view> }
```

`GET /cases/{case_id}/download` → `{url}` (the case's YAML document).

## Super-admin

| Route | Body | Response |
|---|---|---|
| `GET /admin/settings` | — | `{settings}` — flat: every engine setting, `month_budget`, `user_day_budget`, `case_default`, `batch_default`, `case_ceiling`, `batch_ceiling`, `infra_turn`, `infra_case`, and `cascades: {rudi, tester, judge: [entry …]}` |
| `PUT /admin/settings` | `{settings}` (same shape, any subset) | `{settings, changed: [text …]}` — 400 `bad_setting` / `bad_cascade` |
| `POST /admin/engine/{pause\|resume\|drain\|kill}` | — | `{state}` |
| `GET /admin/users` | — | `{users: [{u, role, status, last, batches, created, by, note, spentToday}]}` |
| `POST /admin/users` | `{username, role}` | 201 `{temp_password}` |
| `POST /admin/users/{name}/reset` | — | `{temp_password}` |
| `POST /admin/users/{name}/lock` · `/unlock` | — | `{ok}` |
| `POST /admin/users/{name}/role` | `{role}` | `{ok}` |
| `DELETE /admin/users/{name}` | — | `{ok}` — 409 for yourself or the last super-admin |
| `GET /admin/audit?kind=&days=14` | — | `{audit: [[when, who, action, target, detail] …]}` |
| `GET /admin/storage` | — | `{storage: [[prefix, holds, size, kept] …], bucket}` |
| `GET /admin/versions` | — | `{versions: [[component, version, since] …]}` |

A cascade entry:

```
{ name, model, provider, kind: "openai_compatible"|"groq", endpoint, key, on, in, out,
  basis: "list"|"estimate"|"free tier", checked, limits: {rpm, rpd, tpm, tpd},
  max_tokens_param, params, note }
```

The API refuses live Rudi's keys anywhere, Rudi entries whose key is not under
`meetrudi/arena/rudi-`, and tester / judge entries that use a Rudi key.
