# Rudi Operator Console (frontend)

Static operator console for 1:1 WhatsApp conversations. Roster on the left, WhatsApp-style
thread on the right, type-to-reply composer. Talks to `meetrudi-wa-console-api`.

> **Not** hosted on the public GitHub Pages site — it shows PII (names, phones, message
> content). Target hosting: private S3 + CloudFront behind Cognito (auth block).

## Run locally (DEMO mode — no backend)
Leave `config.js` `API_BASE` empty and open `index.html` (a static server is fine):
```cmd
python -m http.server 8080 --directory console
```
Then browse http://localhost:8080/ — it runs on synthetic data from `demo-data.js`
(fictional contacts, no real PII). Try switching conversations, sending in an open window, and
the out-of-window (Carla) conversation which blocks free-form replies.

## Point at the live API
`config.js` holds the interim console token, so it is **gitignored** — it must never be committed.
Copy the template and fill it in locally:
```cmd
copy console\config.example.js console\config.js
```
Then set `API_BASE`, `CONSOLE_TOKEN` (the `meetrudi/whatsapp/console-token` value), and `OPERATOR_ID`
in your local `config.js`.

## Files
- `index.html` — operator console UI + logic (vanilla JS, brand tokens inline; matches `visuals/brand.css`).
- `config.example.js` — **tracked** template (no secrets); copy to `config.js`.
- `config.js` — local runtime config (API base, token, poll cadence). **Gitignored — never commit.**
- `demo-data.js` — synthetic backend for the operator-console DEMO mode.

## Personality test console — **public**, lives in `site/test-console/`
A separate, login-gated console for **mass-testing Rudi personalities** — it drives the *same*
responder engine (no Twilio) against isolated `test-conversations/` S3 data. Unlike this operator
console (which shows PII and stays local), the test console uses only synthetic test data, so it is
published to GitHub Pages:

    https://meet-rudi.github.io/start-up-app/test-console/

It is served from `site/test-console/` (the only folder GitHub Pages publishes — see
`.github/workflows/pages.yml`). To wire it: set `API_BASE` in `site/test-console/test-config.js` to
the `meetrudi-test-console` Function URL (deploy.py prints it as "Test console"), commit, and push —
the Pages workflow redeploys on any `site/**` change. Log in with the email+password stored in the
`meetrudi/test-console/auth` secret. Leave `API_BASE` empty to fall back to a synthetic DEMO
(login password `demo`).
- `site/test-console/index.html` — cards overview, new-conversation launcher, WhatsApp-style chat modal, `.md` export.
- `site/test-console/test-config.js` — `API_BASE` only (no secret; auth is the login).
- `site/test-console/test-demo.js` — synthetic DEMO backend (canned replies).
