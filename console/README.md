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
- `index.html` — UI + logic (vanilla JS, brand tokens inline; matches `visuals/brand.css`).
- `config.example.js` — **tracked** template (no secrets); copy to `config.js`.
- `config.js` — local runtime config (API base, token, poll cadence). **Gitignored — never commit.**
- `demo-data.js` — synthetic backend for DEMO mode (Block-3 development without integrations).
