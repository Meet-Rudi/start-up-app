# meetrudi-training-arena

Mass-simulated conversations with the deployed Rudi engine. Design and decisions:
[docs/training-arena.md](../../docs/training-arena.md); file formats:
[docs/training-arena-formats.md](../../docs/training-arena-formats.md); API contract: [API.md](API.md).

## Layout

| Path | What |
|---|---|
| `src/api.py` | `meetrudi-arena-api` — the console's only door (Function URL) |
| `src/ingest.py` | `meetrudi-arena-ingest` — validates an input file, lays out its cases |
| `src/dispatcher.py` | `meetrudi-arena-dispatcher` — pace, budgets, progress; single writer |
| `src/runner.py` | `meetrudi-arena-case-runner` — one case, one conversation, checkpointed per step |
| `src/assembler.py` | `meetrudi-arena-assembler` — plans the results parts, then streams them |
| `src/arena_format.py` | the input format: read, validate, resolve, estimate |
| `src/arena_results.py` | the results format: render, plan parts, index CSV |
| `src/arena_llm.py` | the arena's own model client: preventive failover, limit tracking |
| `src/arena_eval.py` | evaluators: LLM judge, decision models (System One, classifiers), routing |
| `src/arena_tester.py` | the simulated user |
| `src/arena_costs.py` | prices, AWS overhead, budget arithmetic |
| `src/arena_auth.py`, `arena_store.py`, `arena_common.py` | accounts, bucket layout, helpers |
| `seed/` | copied to the arena bucket on deploy: `defaults/` (settings, cascades, evaluators) and `prompts/` |
| `build_layer.py` | builds the PyYAML layer (`layer/`, git-ignored) |
| `tests/` | `python -m unittest discover -s services/training-arena/tests -v` |

Rudi's replies come from `meetrudi-engine-turn` in the `meetrudi-whatsapp` stack
(`services/whatsapp/src/engine_turn.py`); this folder holds no copy of Rudi.

## Deploy

After the one-time console steps in [infra/iam/README.md](../../infra/iam/README.md) §7–8:

```cmd
python deploy.py whatsapp
python deploy.py training-arena
```

Then put the printed API URL into `site/training-arena/config.js` (`API_BASE`) and push.

## Run a batch by hand

Copy a file into the inbox, then submit it from the console's **New batch → Point to S3**:

```cmd
aws s3 cp evals/arena-batches/crisis-signposting__input.yaml ^
  s3://meetrudi-arena-data-949753869755/inbox/<your-username>/crisis-signposting__input.yaml ^
  --region eu-central-1 --profile rudi-deployer
```
