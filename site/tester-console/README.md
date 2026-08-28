# site/tester-console — the pages testers see

Six static pages, no build step, no framework, no dependencies. They ship to GitHub Pages exactly
as they are. Product context and the deploy sequence live in
[docs/tester-console.md](../../docs/tester-console.md).

| File | What it is |
|---|---|
| `register.html` | Sign-up. Language switch, Belgian-mobile validation, name-sanity check, both consents, honeypot. |
| `check-your-email.html` | "We sent you a link." Displays the address only; holds no credential. |
| `set-password.html` | Serves both first-time verification (`?t=…`) and reset (`?t=…&mode=reset`). Signs the tester straight in. |
| `index.html` | Login, plus the forgotten-password form. The landing page. |
| `console.html` | The three tracks, the do's-and-don'ts modal, the session clock. |
| `admin.html` | Internal control room: KPIs, roster, actions, settings, CSV export. |
| `config.js` | **The only file to edit after deploying.** |
| `rudi.css` / `rudi.js` / `i18n.js` | Shared styling, client helpers, and every user-facing string. |

## After deploying

Put the `TesterApiUrl` output of the `meetrudi-whatsapp` stack into `config.js` as `API_BASE`,
then push. Nothing else in this directory needs changing.

```cmd
aws cloudformation describe-stacks --stack-name meetrudi-whatsapp ^
  --region eu-central-1 --profile rudi-deployer ^
  --query "Stacks[0].Outputs[?OutputKey=='TesterApiUrl'].OutputValue" --output text
```

## Rules these pages keep

- **Nothing secret ships here.** These files are world-readable. `config.js` holds a public API
  base and nothing else — no token, no key, no phone number.
- **The client authorises nothing.** Session validity, idle expiry, the call quota, quiet hours
  and the admin role are all decided by `meetrudi-tester-api`. A hidden button here is a courtesy
  to the tester, never a control.
- **No hard-coded sentences.** Every string a tester reads comes from `i18n.js`, keyed by locale.
  Adding French is one object, not a rewrite.
- **The assigned call goal never arrives.** `/me` omits it by construction, so it cannot leak
  through the page even by accident.

## Working on them locally

The pages need a real API to talk to. The quickest honest way to see them work is to point
`config.js` at a deployed `TesterApiUrl` and open them from any static server:

```cmd
python -m http.server 8080 --directory site/tester-console
```

Opening them as `file://` will not work — the API calls are cross-origin and the browser blocks
them.
