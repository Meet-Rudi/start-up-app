-- MEET_RUDI platform — 0004: metering (calls, budgets), alerts, erasures, jobs.
--
-- Target: PlanetScale Vitess (MySQL 8), eu-central-1 (decision #46). Never edit a migration that
-- has run anywhere; add the next number instead. Design: docs/platform/blueprint.md §6, §7, §10,
-- §11 and decision #42. Ids, labels and numbers only: nothing here holds what anyone said.
--
-- tenant_id NULL is a platform row (a platform call, alert or job); platform work reads those
-- with `tenant_id IS NULL`, tenant work with its own tenant (decision #47).

-- ----------------------------------------------------------------------------------- metering
-- One metered model or skill call: rudi_platform.gateway.UsageRecord. Per-user cost is observable
-- from here (CLAUDE.md §4, target ≤ €13 per active user a month). A plain table for now;
-- partitions once volume asks for them (decision #42). An erased end-user's calls stay, without
-- the end-user id.
CREATE TABLE metering_calls (
  id                CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^use_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id         CHAR(36) CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id          CHAR(36) CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_version_id  CHAR(36)
                    CHECK (REGEXP_LIKE(clone_version_id, '^cvr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  skill             VARCHAR(128),  -- slug@x.y.z
  channel           VARCHAR(16)
                    CHECK (channel IN ('whatsapp', 'voice', 'api', 'widget', 'sms', 'email', 'telegram')),
  end_user_id       CHAR(36) CHECK (REGEXP_LIKE(end_user_id, '^usr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  turn_id           CHAR(36) CHECK (REGEXP_LIKE(turn_id, '^trn_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  kind              VARCHAR(16) NOT NULL DEFAULT 'model' CHECK (kind IN ('model', 'skill')),
  outcome           VARCHAR(16) NOT NULL
                    CHECK (outcome IN ('ok', 'rate_limited', 'failed', 'out_of_budget')),
  endpoint          VARCHAR(128),
  model             VARCHAR(128),
  region            VARCHAR(32),
  attempts          INT NOT NULL DEFAULT 1 CHECK (attempts >= 0),
  tokens_in         INT NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
  tokens_out        INT NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
  cost_eur          DECIMAL(12, 6) NOT NULL DEFAULT 0 CHECK (cost_eur >= 0),
  latency_ms        INT NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
  occurred_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY calls_tenant_idx (tenant_id, occurred_at DESC),
  KEY calls_clone_idx (tenant_id, clone_id, occurred_at DESC),
  KEY calls_end_user_idx (tenant_id, end_user_id, occurred_at DESC),
  KEY calls_turn_idx (tenant_id, turn_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- A spend budget for a tenant or one clone, optionally per end-user (heavy users); it alerts,
-- and stops serving only when the kill switch is on (blueprint §6, §10).
CREATE TABLE metering_budgets (
  id             CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^bud_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id      CHAR(36) NOT NULL
                 CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id       CHAR(36)  -- NULL = the whole tenant
                 CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  budget_period  VARCHAR(8) NOT NULL CHECK (budget_period IN ('day', 'month')),
  limit_eur      DECIMAL(12, 2) NOT NULL CHECK (limit_eur > 0),
  per_end_user   BOOLEAN NOT NULL DEFAULT FALSE,
  alert_pct      INT NOT NULL DEFAULT 80 CHECK (alert_pct BETWEEN 1 AND 100),
  kill_switch    BOOLEAN NOT NULL DEFAULT FALSE,
  created_by     CHAR(36) CHECK (REGEXP_LIKE(created_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY budgets_tenant_idx (tenant_id, clone_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- ---------------------------------------------------------------------------------------- ops
-- A raised alert: spend, errors, safety or quota.
CREATE TABLE ops_alerts (
  id               CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^alr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id        CHAR(36) CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  kind             VARCHAR(16) NOT NULL CHECK (kind IN ('spend', 'errors', 'safety', 'quota')),
  budget_id        CHAR(36) CHECK (REGEXP_LIKE(budget_id, '^bud_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  detail           JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- ids and numbers only
  raised_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  acknowledged_by  CHAR(36)
                   CHECK (REGEXP_LIKE(acknowledged_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  acknowledged_at  DATETIME(6),
  PRIMARY KEY (id),
  KEY alerts_tenant_idx (tenant_id, raised_at DESC),
  KEY alerts_budget_idx (tenant_id, budget_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- An erasure request and its receipt (blueprint §7.3; CLAUDE.md §5: complete, not best-effort).
-- The end-user id is held only while the erasure runs; once it is done only its SHA-256 stays,
-- so the receipt can be matched to a request without keeping the pseudonym.
CREATE TABLE ops_erasures (
  id             CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^era_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id      CHAR(36) NOT NULL
                 CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  end_user_id    CHAR(36) CHECK (REGEXP_LIKE(end_user_id, '^usr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  subject_hash   VARBINARY(32) NOT NULL CHECK (LENGTH(subject_hash) = 32),
  requested_by   CHAR(36)  -- NULL = through the API
                 CHECK (REGEXP_LIKE(requested_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  requested_via  VARCHAR(16) NOT NULL CHECK (requested_via IN ('api', 'console', 'operator')),
  status         VARCHAR(16) NOT NULL DEFAULT 'requested'
                 CHECK (status IN ('requested', 'running', 'done', 'failed')),
  receipt        JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- per store: what was removed (counts)
  requested_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  completed_at   DATETIME(6),
  PRIMARY KEY (id),
  KEY erasures_tenant_idx (tenant_id, requested_at DESC),
  KEY erasures_open_idx (tenant_id, status, requested_at),
  CHECK (status <> 'done' OR (end_user_id IS NULL AND completed_at IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- A chain of Lambda runs doing one piece of long work (conventions §3: hand over before 13
-- minutes, at most 72 hops).
CREATE TABLE ops_jobs (
  id             CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^job_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id      CHAR(36) CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  kind           VARCHAR(64) NOT NULL CHECK (REGEXP_LIKE(kind, '^[a-z0-9]+([-.][a-z0-9]+)*$', 'c')),
  function_name  VARCHAR(64) NOT NULL
                 CHECK (REGEXP_LIKE(function_name, '^meetrudi-(internal|tenant)-', 'c')),
  status         VARCHAR(16) NOT NULL DEFAULT 'running'
                 CHECK (status IN ('running', 'done', 'failed', 'cancelled')),
  hops           INT NOT NULL DEFAULT 0 CHECK (hops BETWEEN 0 AND 72),
  checkpoint_id  CHAR(36) CHECK (REGEXP_LIKE(checkpoint_id, '^chk_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  error_kind     VARCHAR(128),
  started_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  finished_at    DATETIME(6),
  PRIMARY KEY (id),
  KEY jobs_open_idx (tenant_id, status, updated_at),
  KEY jobs_tenant_idx (tenant_id, started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
