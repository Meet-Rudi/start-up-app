# MEET_RUDI — Project Blueprint

> Human-facing blueprint (the *why* and *what*). Coding rules and conventions live in
> [CLAUDE.md](CLAUDE.md) (the *how*), which Claude Code auto-loads each session.
> Last updated: 2026-06-04.

---

## 1. Mission & Non-Goals

**Mission.** MEET_RUDI ("Rudi") helps people adhere to a prescribed *lifestyle* (movement,
diet, daily activity, and related habits) in order to support their recovery or treatment.
Rudi does this by engaging each person in regular, natural conversation — feeling and
behaving like one of their everyday chat buddies on WhatsApp.

**How we help.** Ongoing, AI-generated conversation that:
- asks about and reflects on the user's activity, movement, diet, and lifestyle;
- remembers each user's case and how it develops over time (long-term memory);
- understands photos of food (computer vision) and audio messages;
- (later) ingests data points from smart wearables.

### Non-Goals / Hard Guardrails
- **Not a medical device.** We do not diagnose, treat, or make clinical claims. Design so
  this line is never *accidentally* crossed. ("Not for now" is an explicit product stance,
  not a deferred ambition.)
- **No medical advice.** Rudi supports adherence to a plan a clinician already set; it does
  not invent medical recommendations. When a user asks for medical advice or reports
  red-flag symptoms, Rudi deflects and (where applicable) escalates — see §7 Guardrails.
- **No unsolicited mass messaging.** All proactive contact respects WhatsApp policy and the
  user's opt-in (see §4).

---

## 2. Users, Pilot & Consent

- **First cohort:** Type-2-diabetes (T2D) patients via a **pilot organization**, primarily in
  **Belgium**. T2D lifestyle adherence (diet, movement, glucose-related habits, medication
  adherence) is therefore our **first guideline domain**.
- **Onboarding model:** the **user reaches out first** — they add/invite the Rudi account as a
  contact and send the first message. That first inbound message opens the initial
  conversation window.
- **Consent:** health data is **special-category data** (GDPR Art. 9). Enrollment must capture
  explicit, informed, revocable consent before any health-related processing. Consent state
  is part of the user record and gates processing.
- **Relationship ownership:** for the pilot, the patient relationship is mediated by the pilot
  organization; clarify data-controller vs data-processor roles in the DPA. *(Open: confirm
  controller/processor split with the pilot org.)*

---

## 3. Product Model — The Rudi Experience

- **Channel:** WhatsApp is the primary (initially only) channel. Rudi should feel like a
  regular chat buddy: text, images, and audio messages.
- **Personas:** several "Rudis," each on its **own WhatsApp number**, differing by *persona*
  (not for throughput). **A user is pinned to one Rudi/number for life** (barring explicit
  migration) — required for memory continuity and per-number quality-rating management.
- **Proactive cadence:** Rudi initiates **~2–3× per week** when the user has gone quiet;
  otherwise it responds reactively. (See §4 for the WhatsApp constraint this rides on.)
- **Language:** **English-first for the pilot**, but built **i18n-ready** from day one
  (no hard-coded user-facing strings; locale on the user profile; prompts & templates keyed
  by language) so Dutch/French is a content task, not a rewrite.

---

## 4. WhatsApp Constraints (load-bearing — read before designing the conversation engine)

WhatsApp Business does **not** allow free-form proactive messaging. The conversation engine
therefore has two explicit modes:

- **In-window (free-form):** within **24h** of the user's last inbound message, Rudi can send
  rich, free-form, full-persona replies.
- **Out-of-window (templated):** after 24h of user silence, Rudi may send **only pre-approved
  template messages**. Proactive check-ins use a lightweight templated "tap on the shoulder"
  ("Rudi has a question for you 👋 — reply to continue"); the rich conversation resumes once
  the user replies and reopens the window.

Other rules baked into the design:
- **Quality rating & messaging tier are per-number.** Reports/blocks degrade a number → it
  gets throttled or banned. We protect each number's rating; we do **not** spread users across
  numbers to evade limits.
- **Access path:** start via a **BSP (Twilio-like)** for the PoC; migrate to **Meta Cloud API
  direct** after PoC. The WhatsApp provider is hidden behind an interface so this swap touches
  no business logic (see CLAUDE.md).

---

## 5. High-Level Architecture

Two cooperating planes:

### App / Orchestration Plane — AWS (EU region)
- **Event-driven & async.** Relaxed latency (10–15s acceptable) lets us use queues/events
  (SQS), batching, and tolerate slower/cheaper inference.
- **Compute:** **Lambda** (container images) for webhook ingest and short tasks; **Fargate**
  for long-running work (orchestrator, model serving shims, workers).
- **Datastore (PoC):** a **single Postgres (RDS/Aurora) with `pgvector`** holding user profile,
  event log, and vectors. Split out a dedicated vector DB only when recall volume demands it.
- **Source of truth** for the user "case": the structured profile + event log in Postgres;
  vectors and cached summaries are derived/rebuildable.

### Inference / GPU Plane — Neo-cloud (EU region)
- AWS will not grant GPU quota to a new startup, so self-hosted GPU inference runs on a
  **neo-cloud** (CoreWeave / Lambda / RunPod / etc.), provisioned via its own IaC/API.
- **PII boundary:** PII/health data **stays in the AWS EU plane**. The GPU plane receives only
  **minimized, pseudonymized** payloads (IDs not names). EU-region GPU + signed DPA required.

```
WhatsApp ⇄ BSP(Twilio→Meta) ⇄ [AWS EU: webhook(Lambda) → queue → orchestrator(Fargate)
                                 → memory(pgvector) → model gateway] ⇄ [Neo-cloud EU GPU:
                                 embeddings, self-hosted reasoning model]  (pseudonymized only)
                                                     │
                                          3rd-party LLM/ASR/vision APIs (EU-eligible)
```

---

## 6. AI Architecture

- **Orchestration:** **own orchestration agents**, framework-light. Reconsider a framework
  (e.g. LangGraph) only if real pain emerges.
- **Per-message pipeline (conceptual):** ingest → (ASR if audio / vision if image) →
  context assembly (profile + recent summary + vector recall) → reasoning LLM →
  **guardrail gate** → send (respecting window mode).
- **Memory (three layers):**
  1. **Structured user profile** — goals, constraints, plan, locale, consent, key facts.
  2. **Cached recent state / summary** — short rolling summary of recent issues & state,
     cheap to inject into prompts.
  3. **Vector store** — semantic long-term recall of past conversation/events.
- **Model strategy — phased, behind a gateway:**
  - PoC: **reasoning on 3rd-party API** (cheap to start, zero ops).
  - Then move **embeddings** to self-hosted (steady, high-volume, easy win).
  - Move **reasoning** to a self-hosted open-weight model on neo-cloud **only when utilization
    justifies the always-on GPU cost**. Frontier-grade is not required — good guardrails carry
    quality for lifestyle conversation.
  - A **model-agnostic gateway** lets us swap **API ↔ self-hosted** per workload without
    touching business logic.

---

## 7. Guardrails ("double-checking against guidelines" without giving medical advice)

Layered, automated-first:
1. **RAG grounding** over a **curated guideline corpus** (starting with T2D lifestyle guidance)
   to keep responses consistent with vetted material.
2. **ML classifiers on key risk aspects** — detect e.g. hypoglycemia, disordered-eating
   signals, and other red-flag content → trigger deflection/escalation and suppress
   advice-like output.
3. **Knowledge graph** — used **only when a case genuinely needs** structured reasoning;
   not a default dependency.
4. **Medical-advice refusal pattern** — when asked for diagnosis/treatment, Rudi stays in the
   adherence-support lane and points the user back to their clinician.
5. **Periodic human spot-checks** of sampled conversations for safety/quality (operational,
   not in the live send path).

---

## 8. Compliance Posture

- **GDPR-first**, EU data residency; AWS EU region; neo-cloud EU region.
- Health data = **Art. 9 special-category** → explicit consent, data minimization, purpose
  limitation, right to erasure honored across all stores (incl. vectors & caches).
- **PII never leaves the AWS EU plane** except as pseudonymized payloads to the EU GPU plane
  under a DPA.
- DPAs required with: BSP (Twilio/Meta), any 3rd-party AI API used, and the neo-cloud vendor.
- *(Open: data-controller/processor mapping with the pilot org; DPIA for the pilot.)*

---

## 9. Cost Model & Unit Economics

- **Revenue:** ~**€20 / user / month** (expected).
- **Cost ceiling:** **≤ €13 / active user / month** (AI + infra), targeting **~€7 margin**.
- **Risk:** *heavy* users (lots of images/audio, long chats) blowing past the average — not the
  ceiling itself. Mitigate with caching, batching, right-sized models, and per-user cost
  observability.
- **Why self-hosting is phased:** an always-on GPU is *more* expensive per user at low volume;
  the economics flip only at high, steady utilization. API-first protects early unit economics.

---

## 10. Roadmap / Phasing

- **Phase 0 — Auxiliary side deliverables** (precede the main MVP; to be specified next).
- **Phase 1 — PoC:** text-first WhatsApp loop via Twilio, single persona, English, memory
  (pgvector), reasoning on API, basic RAG guardrail, EU AWS.
- **Phase 2 — Pilot:** T2D cohort; add audio (ASR) + food vision; risk classifiers; templated
  proactive cadence; cost observability; consent/DPIA in place.
- **Phase 3 — Scale & cost:** self-host embeddings, then reasoning on neo-cloud GPU; Meta Cloud
  API direct; multi-persona; Dutch/French localization.
- **Phase 4 — Wearables:** integrate device data points.

---

## 11. Decisions Log (settled)

| # | Decision |
|---|----------|
| 1 | WhatsApp via **Twilio (BSP)** first → **Meta Cloud API direct** later; hidden behind a provider interface. |
| 2 | **Multi-persona**, one number per persona; **one user → one fixed Rudi for life**. |
| 3 | Proactive cadence **~2–3×/week**; in-window free-form vs out-of-window templated modes. |
| 4 | **GDPR-first**, EU residency, AWS EU region + neo-cloud EU region. |
| 5 | **Not a medical device / no medical advice** — explicit product guardrails. |
| 6 | Stack: **Python** (AI/messaging core) + **TypeScript/JS** (front-end). **Mono-repo.** |
| 7 | **2 senior engineers**, greenfield. |
| 8 | AWS **SAM/CloudFormation**, **Lambda container images**; CFN/Terraform for Fargate; neo-cloud own IaC. |
| 9 | **Own orchestration**, framework-light. |
| 10 | Memory = **pgvector + cached summary + structured profile**; Postgres is source of truth. |
| 11 | **Self-host embeddings + mid-size open-weight reasoning model** for cost; **API-first**, phased. |
| 12 | **Relaxed latency** (10–15s) → async/queue-based design. |
| 13 | **English-first, i18n-ready**. |
| 14 | Guardrails = **RAG + ML risk classifiers + (optional) knowledge graph** + human spot-checks. |
| 15 | **Neo-cloud for GPU** (AWS denies GPU quota to new startups); **PII stays in AWS EU plane**, pseudonymized payloads to GPU plane. |
| 16 | Unit economics: ~€20 revenue, **≤ €13 cost/active-user/mo**, ~€7 margin. |

## 12. Open Questions

- Data-controller vs processor split with the pilot org; DPIA scope.
- Which 3rd-party LLM/ASR/vision APIs are EU-residency-eligible and acceptable.
- Neo-cloud vendor selection (EU region + DPA + GPU availability).
- Heavy-user cost containment policy (caps? fair-use? tiering?).
- Specification of the Phase-0 auxiliary side deliverables.
- **Google for Startups Cloud credits** — explore via **Start it @KBC / imec.istart** (likely
  Google partners → Scale/AI tier, ~$100k–$350k in credits). Eligibility: for-profit, young
  (<10 yrs), ≤ ~Series A, new to the program; partner affiliation unlocks the larger tiers.
  **Data residency is NOT set by the credits** — to stay GDPR-clean we must use **Google Cloud
  EU regions** (e.g. `europe-west1`, St. Ghislain BE) + **Assured Workloads (EU Regions &
  Support)** + org-policy location constraints, and **Vertex AI in an EU region** (the free
  AI Studio Gemini endpoint is US-processed). Verify each model's EU-region availability.
  Use credits as a **non-PII prototyping subsidy** (model eval, food-vision, batch); keep the
  patient-data plane on the AWS EU + neo-cloud stack.
