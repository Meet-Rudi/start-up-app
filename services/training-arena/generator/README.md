# Training Arena Generator (local)

Describe a skill in the console's **Generator** pane (super-admins only); Claude Code on **your own
computer** plans and writes a batch file the arena can run straight away, checked with the arena's
own validator. It runs on your **Claude subscription** — no API key, nothing in AWS.

## Start it

Once per session, in Windows `cmd`, from the repository folder:

```cmd
python services\training-arena\generator\generator.py
```

It checks that Claude Code answers on your subscription, then prints a **pairing code**. In the
console: **Generator → the connection pill → enter the code**. If the browser asks whether the arena
site may reach devices on your network, allow it — the generator listens on this computer only.

Needs: Claude Code installed and signed in with your Claude plan (`claude`, then `/login`), Python
3.10+ with PyYAML (`pip install pyyaml`).

| Option | Default | |
|---|---|---|
| `--port` | 47815 | If you change it, enter `http://127.0.0.1:<port>` as the address in the connection window. |
| `--repo-folder` | `evals\arena-batches\generated` | Where "Save into the repo folder" writes. |
| `--home` | `%USERPROFILE%\.meetrudi\arena-generator` | History: every finished file, reopenable from the console. |
| `--origin` | — | Another console address allowed to use it (the Pages site and localhost always are). |
| `--skip-check` | off | Skip the sign-in check at start. |

## What it does

1. **Plan** — the generator lays out the slots itself (how many cases, which mode and language each,
   so the mix is exactly what you asked for); Claude fills each slot with a persona, a title and what
   it probes. You can change a mode, have Claude redo a line, or drop one.
2. **Write** — Claude writes the cases five at a time, as JSON; the generator renders the YAML (so
   quoting and indentation are never Claude's to get wrong). Scripted cases get an end-of-case
   `expect`, goal-based ones `success_when` and, for clear violations, `abort_when`.
3. **Check** — the arena's own validator (`../src/arena_format.py`, the code that runs in AWS) checks
   the file; a failing case goes back to Claude with the validator's messages, twice at most, and is
   left out (and listed) after that.
4. **Save** — the console uploads it to the arena inbox through the arena API (and can create the
   batch in the same click), downloads it, or has the generator write it into the repo folder.

Code: `gen_core.py` (the pipeline, pure; Claude is injected), `generator.py` (the local server and
Claude Code), `personas.yaml` (the persona library). Tests: `../tests/test_generator.py`.

## Safety and data

- **Your subscription, never an API key.** Claude Code runs as `claude -p` with no tools, no MCP
  servers and no project settings, in an empty temporary folder. `ANTHROPIC_API_KEY` and the other
  API / cloud-provider variables are removed from its environment.
- **Synthetic only.** Sent to Claude: your description (refused if it holds a real-looking phone
  number, e-mail address or ID number), the format rules, the persona sheet. Never Rudi's prompts,
  transcripts, results or keys. This is the approved Prime Directive 3 exception
  (project.md, decision 21).
- **Only you can drive it.** It listens on `127.0.0.1` only, answers only the arena console's origin
  and localhost, refuses other Host headers, and needs the token that pairing returns; ten wrong
  codes lock pairing until it restarts.
- **One generation at a time** — it is one person's subscription and its usage limits.
