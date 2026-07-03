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
Edit `config.js`:
```js
window.CONSOLE_CONFIG = {
  API_BASE: "https://<console-api-id>.lambda-url.eu-central-1.on.aws",
  CONSOLE_TOKEN: "<the meetrudi/whatsapp/console-token value>",  // interim, until Cognito
  OPERATOR_ID: "your-name",
  POLL_MS: 4000,
};
```

## Files
- `index.html` — UI + logic (vanilla JS, brand tokens inline; matches `visuals/brand.css`).
- `config.js` — runtime config (API base, token, poll cadence).
- `demo-data.js` — synthetic backend for DEMO mode (Block-3 development without integrations).
