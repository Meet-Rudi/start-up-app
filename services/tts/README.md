# meetrudi-tts — Piper text-to-speech on CPU Lambda

Self-hosted voice. No GPU, no per-character billing, no US processor: the weights sit in our own
EU bucket and synthesis happens inside a Lambda in `eu-central-1`.

It exists because **the Dutch requirement eliminated every managed EU voice API.** OVHcloud's
catalogue stops at en/de/es/it, Scaleway and IONOS host no TTS at all, and Groq's Orpheus is
English-only. For Dutch the choice was self-host or buy American — so we self-host.

## Voices

Short keys, so callers never hardcode a filename:

| Key | Model | Note |
|---|---|---|
| `en`, `en_US` | `en_US-lessac-medium` | |
| **`nl`, `nl_BE`** | **`nl_BE-nathalie-medium`** | **Belgian Flemish** |
| `nl_NL` | `nl_NL-mls-medium` | Netherlands Dutch |
| `fr` | `fr_FR-siwis-medium` | |
| `de` | `de_DE-thorsten-medium` | |

> **`nl` deliberately resolves to Flemish, not Netherlands Dutch.** The pilot cohort is Belgian,
> and a Netherlands-Dutch voice reads as audibly foreign to a Flemish speaker. For a product
> whose proposition is "feels like a chat buddy", that is a defect, not an accent preference.
> A test asserts this mapping so nobody quietly "fixes" it later.

All voices are MIT-licensed, from [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).

## Shape

```
caller ──POST {token,text,voice}──▶ meetrudi-tts (Lambda, 2GB, python3.12)
                                      │  layer: meetrudi-piper  (125.6 MB)
                                      │    onnxruntime · espeak bridge · numpy
                                      │
                                      ├─ voice in /tmp?  ──yes──▶ synthesize
                                      └─ no ──▶ pull 60-73 MB from
                                               s3://<data-bucket>/voices/piper/
                                               ──▶ cache in /tmp ──▶ synthesize
                                      │
                            ◀──{audio_b64, mime, timings}──
```

**Why a separate service** rather than folding Piper into `meetrudi-voice-bench`: Piper needs a
125 MB native layer, and the bench is a fast dependency-free zip deploy we want to keep
iterating on. Splitting them also makes the voice a swappable component for Plan A.

**Why a layer and not a container image:** the deploy workstation has no Docker, and CLAUDE.md
§7's container-image preference is unbuildable without it. `pip --platform manylinux_2_28_x86_64`
produces Lambda-compatible binaries from Windows, which `build_layer.py` automates.

### Size budget

Lambda allows 250 MB unzipped across function and layers:

| | |
|---|---|
| onnxruntime | 53 MB |
| piper (after dropping Hebrew models) | 24 MB |
| numpy | 58 MB |
| rest | ~2 MB |
| **total** | **125.6 MB** — ~124 MB headroom |

`piper/hebrew/` is pruned because it is imported lazily. `piper/tashkeel/` is **not** pruned —
`piper/voice.py` imports it at module level and the layer breaks without it.

Voice models are excluded: five voices are 313 MB on their own, which is why they live in S3
and land in `/tmp` (1 GB ephemeral) on first use.

## API

```jsonc
// POST to the Function URL
{"token": "...", "text": "Hallo Filip", "voice": "nl", "length_scale": 1.0}

// 200
{"ok": true, "audio_b64": "...", "mime": "audio/wav", "voice": "nl_BE-nathalie-medium",
 "sample_rate": 22050, "chars": 11, "timings": {"load_ms": 0, "synth_ms": 210, "total_ms": 214}}

// health check — no token, synthesises nothing
{"ping": true}
```

The Function URL is public, so a shared token in `meetrudi/tts/token` guards it. The function
**fails closed**: every synthesis request returns `503` until that secret exists.

## Deploy

```cmd
python deploy.py tts
```

`deploy.py` runs `build_layer.py` first, so the native wheels are always rebuilt for Lambda's
platform before SAM packages them.

Two one-time steps:

```cmd
python services/tts/seed_voices.py
```

and create the auth secret in the console — `rudi-deployer` is not permitted to create secrets
(§7). See [`infra/iam/README.md`](../../infra/iam/README.md) §4.

Verify with:

```cmd
curl -X POST <function-url> -H "Content-Type: application/json" -d "{\"ping\":true}"
```

## Cost

Lambda at 2 GB is $0.0000333/second. A typical 8-minute call has Rudi speaking ~2,500
characters; Piper synthesises roughly 6× faster than real time on CPU, so about 25 seconds of
compute — **~€0.0008 per call**, plus a ~2.5s cold start on the first call per container.

For comparison, the same call costs ~€0.055 on Groq Orpheus. Piper is roughly **65× cheaper**
and keeps the audio in the EU. It also sounds markedly more synthetic — which is the trade this
service exists to let you actually hear.

## Known limits

- **Robotic.** Piper is a fast VITS model, not a speech LLM. Judge it against the product, not
  against Orpheus.
- **Cold start ~2.5s** — Lambda init, a 60 MB S3 pull, then the ONNX load. Warm calls skip all
  three. For scheduled outbound this is schedulable; for the bench it hits the first turn only.
- **No voice cloning.** Fixed speakers. Chatterbox is the option if a cloned Flemish voice
  matters more than the cost.
- **CloudShell fallback:** if a future wheel refuses to resolve for manylinux, run
  `build_layer.py` in AWS CloudShell (Amazon Linux, so plain `pip install --target` works),
  zip `layer/`, and upload it.
