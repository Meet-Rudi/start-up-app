# Training Arena — console

Static console for mass-simulating conversations with Rudi. Design:
[docs/training-arena.md](../../docs/training-arena.md); file formats:
[docs/training-arena-formats.md](../../docs/training-arena-formats.md); API contract:
[services/training-arena/API.md](../../services/training-arena/API.md).

## Two modes

`config.js` decides, through `API_BASE`:

- **Empty — prototype.** Every button works against `demo-data.js` and nothing leaves the
  browser. This is the state in the repository.
- **Set — live.** `API_BASE` is the `ApiUrl` output of the `meetrudi-training-arena` stack. Sign-in
  goes to `POST /login`, every pane loads what it draws from the API and every action calls it.
  The session token is kept in `sessionStorage` only. The API pins CORS to the GitHub Pages
  origin, so live mode works from the published site, not from a local server.

Money is always a total: model spend plus an estimate of the AWS overhead (a unit price per
turn and per case, set under Engine control). Budgets count both.

## Open it locally (prototype)

From the repository root, in `cmd`:

```cmd
python -m http.server 5599 --bind 127.0.0.1 --directory site
```

Then open <http://127.0.0.1:5599/training-arena/enter_arena.html>.

Sign in with **Demo super-admin** or **Demo tester** (or any username: `admin` is a super-admin,
`newuser` shows the temporary-password step, anything else is a tester).

## Files

| File | Role |
| --- | --- |
| `enter_arena.html` | Sign-in. The entry point. Also asks for a new password after a temporary one. |
| `arena.html` | The console: left menu, one pane per area, cards inside each pane. |
| `arena.css` | Styling — the tester console's brand tokens, copied so this folder stands alone. |
| `arena-api.js` | The one place that talks to the API: adds the session token, sends an ended session back to sign-in, turns refusals into readable errors. |
| `arena.js` | Routing and behaviour. Each pane loads its data before drawing; each demo handler names the API call it stands in for. |
| `demo-data.js` | Synthetic demo data, used only in prototype mode. |
| `config.js` | `API_BASE` — the `meetrudi-arena-api` URL. Not a secret. |
| `samples/` | A worked input file, its two-part results file and its batch index. |

## Panes

Dashboard · New batch · Batches & results · Case viewer · Formats & templates · My account, and
for super-admins only: Engine control · Users · Audit & storage.

The super-admin panes are simply not drawn for a tester. That is presentation, not protection —
this bundle is public. The API re-checks the role on every admin route.

## Publishing

Everything under `site/` is published to GitHub Pages on a push to `main`, so this folder goes
live at `/training-arena/enter_arena.html` with that push. It contains no secrets, no real data,
and `noindex`.
