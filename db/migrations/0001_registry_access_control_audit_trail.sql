-- MEET_RUDI platform — 0001: registry, access_control and audit_trail tables.
--
-- Target: PlanetScale Vitess (MySQL 8), eu-central-1 (decision #46). Never edit a migration that
-- has run anywhere; add the next number instead.
--
-- One database, no schemas: a table's first word names its area (registry_, access_control_,
-- audit_trail_). PlanetScale runs no triggers, stored functions, procedures or events, and a
-- foreign key would stop working at the first shard, so there are none. The rules they would
-- keep (a tenant's slug never changes, published clone versions and their personas are frozen,
-- a role is assigned in its own scope, the audit log is append-only, a reference points at an
-- existing row) are the platform stores' job, held by tests/test_registry.py.
--
-- Ids: every id column holds `{cat}_` + 32 lowercase Crockford base32 characters
-- (conventions §1) and checks the prefix of the table it belongs to or points at. The
-- application mints ids with rudi_platform.ids.
--
-- Tenant isolation (decision #47): every statement on a table with tenant_id names its tenant in
-- its WHERE clause, and the platform's database layer refuses one that does not before it is
-- sent. Every table holding a tenant's data carries its own tenant_id (decision #41), and its
-- indexes lead with it. Text compares exactly (utf8mb4_0900_bin). Times are UTC: PlanetScale's
-- time zone, which cannot be changed.

-- ------------------------------------------------------------------------------------- cells
CREATE TABLE registry_cells (
  id          CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^cel_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  slug        VARCHAR(16) NOT NULL CHECK (REGEXP_LIKE(slug, '^(pool|d-[a-z0-9]{1,10})$', 'c')),
  kind        VARCHAR(16) NOT NULL CHECK (kind IN ('pooled', 'dedicated')),
  region      VARCHAR(32) NOT NULL DEFAULT 'eu-central-1',
  created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY cells_slug_uq (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- ----------------------------------------------------------------------------------- tenants
-- A tenant's slug names its S3 folder, tenants/{slug}/ (decision #41), so it never changes; the
-- registry store refuses a change. The display name may change.
CREATE TABLE registry_tenants (
  id              CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  slug            VARCHAR(63) NOT NULL
                  CHECK (REGEXP_LIKE(slug, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  display_name    VARCHAR(200) NOT NULL,
  cell_id         CHAR(36) NOT NULL
                  CHECK (REGEXP_LIKE(cell_id, '^cel_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  status          VARCHAR(16) NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended', 'offboarding')),
  acceptable_use  JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- exclusions profile (decision #27)
  model_policy    JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- gateway ModelPolicy (blueprint §10)
  limits          JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- rate limits, spend alerts, kill switch
  created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY tenants_slug_uq (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- ---------------------------------------------------------------------------- access_control
CREATE TABLE access_control_accounts (
  id             CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  email          VARCHAR(320) NOT NULL CHECK (email = LOWER(email) AND LOCATE('@', email) > 1),
  display_name   VARCHAR(200) NOT NULL DEFAULT '',
  password_hash  VARCHAR(255),  -- PBKDF2-SHA256, as the arena; NULL for SSO-only accounts later
  status         VARCHAR(16) NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'locked', 'disabled')),
  failed_logins  INT NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
  created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_login_at  DATETIME(6),
  PRIMARY KEY (id),
  UNIQUE KEY accounts_email_uq (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE access_control_permissions (
  id              CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^prm_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  permission_key  VARCHAR(64) NOT NULL
                  CHECK (REGEXP_LIKE(permission_key, '^[a-z]+([.][a-z_]+)+$', 'c')),
  scope_level     VARCHAR(16) NOT NULL CHECK (scope_level IN ('platform', 'tenant')),
  description     VARCHAR(500) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY permissions_key_uq (permission_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE access_control_roles (
  id                          CHAR(36) NOT NULL
                              CHECK (REGEXP_LIKE(id, '^rol_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  role_key                    VARCHAR(64) NOT NULL
                              CHECK (REGEXP_LIKE(role_key, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  display_name                VARCHAR(200) NOT NULL,
  scope_level                 VARCHAR(16) NOT NULL CHECK (scope_level IN ('platform', 'tenant')),
  assignable_by_tenant_admin  BOOLEAN NOT NULL DEFAULT FALSE,
  seed                        BOOLEAN NOT NULL DEFAULT FALSE,
  created_by                  CHAR(36)
                              CHECK (REGEXP_LIKE(created_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at                  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY roles_key_uq (role_key),
  CHECK (scope_level = 'tenant' OR NOT assignable_by_tenant_admin)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE access_control_role_permissions (
  role_id        CHAR(36) NOT NULL
                 CHECK (REGEXP_LIKE(role_id, '^rol_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  permission_id  CHAR(36) NOT NULL
                 CHECK (REGEXP_LIKE(permission_id, '^prm_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  PRIMARY KEY (role_id, permission_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- A platform role is assigned platform-wide (tenant_id NULL), a tenant role in exactly one
-- tenant; the registry store checks the role's scope. MySQL treats NULLs as distinct in a unique
-- key, so tenant_or_platform ('' for platform-wide) keeps one assignment per account, role and
-- tenant.
CREATE TABLE access_control_role_assignments (
  id                  CHAR(36) NOT NULL
                      CHECK (REGEXP_LIKE(id, '^rga_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  account_id          CHAR(36) NOT NULL
                      CHECK (REGEXP_LIKE(account_id, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  role_id             CHAR(36) NOT NULL
                      CHECK (REGEXP_LIKE(role_id, '^rol_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id           CHAR(36) CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  granted_by          CHAR(36)
                      CHECK (REGEXP_LIKE(granted_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  tenant_or_platform  VARCHAR(36) GENERATED ALWAYS AS (COALESCE(tenant_id, '')) STORED NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY role_assignments_uq (account_id, role_id, tenant_or_platform),
  KEY role_assignments_tenant_idx (tenant_id, account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE access_control_sessions (
  id            CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^ses_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  account_id    CHAR(36) NOT NULL
                CHECK (REGEXP_LIKE(account_id, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  token_hash    VARBINARY(32) NOT NULL CHECK (LENGTH(token_hash) = 32),  -- SHA-256, never the token
  created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_seen_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at    DATETIME(6) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY sessions_token_uq (token_hash),
  KEY sessions_account_idx (account_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE access_control_invitations (
  id           CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^inv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id    CHAR(36) NOT NULL
               CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  email        VARCHAR(320) NOT NULL CHECK (email = LOWER(email)),
  role_id      CHAR(36) NOT NULL
               CHECK (REGEXP_LIKE(role_id, '^rol_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  token_hash   VARBINARY(32) NOT NULL CHECK (LENGTH(token_hash) = 32),
  invited_by   CHAR(36) CHECK (REGEXP_LIKE(invited_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  expires_at   DATETIME(6) NOT NULL,
  accepted_at  DATETIME(6),
  PRIMARY KEY (id),
  UNIQUE KEY invitations_token_uq (token_hash),
  KEY invitations_tenant_idx (tenant_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- ------------------------------------------------------------------------- clones and personas
CREATE TABLE registry_clones (
  id               CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id        CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  slug             VARCHAR(63) NOT NULL
                   CHECK (REGEXP_LIKE(slug, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  display_name     VARCHAR(200) NOT NULL,
  purpose          VARCHAR(500) NOT NULL CHECK (CHAR_LENGTH(purpose) BETWEEN 10 AND 500),  -- (T2)
  status           VARCHAR(16) NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'live', 'paused', 'retired')),
  live_version_id  CHAR(36)
                   CHECK (REGEXP_LIKE(live_version_id, '^cvr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY clones_slug_uq (tenant_id, slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- Published clone versions are immutable, and so are their personas (blueprint §4.2): the
-- registry store refuses the change.
CREATE TABLE registry_clone_versions (
  id              CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^cvr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id       CHAR(36) NOT NULL
                  CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id        CHAR(36) NOT NULL
                  CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  version_number  INT NOT NULL CHECK (version_number > 0),
  stage           VARCHAR(16) NOT NULL DEFAULT 'draft'
                  CHECK (stage IN ('draft', 'staging', 'live', 'retired')),
  composition     JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- skill@major pins and overrides
  journey         JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- stage graph
  settings        JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- quiet hours, cadence, retention, channels
  created_by      CHAR(36) CHECK (REGEXP_LIKE(created_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  published_at    DATETIME(6),
  PRIMARY KEY (id),
  UNIQUE KEY clone_versions_number_uq (tenant_id, clone_id, version_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE registry_personas (
  id                CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^per_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id         CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_version_id  CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(clone_version_id, '^cvr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  persona_key       VARCHAR(64) NOT NULL  -- stable; end-users pin it
                    CHECK (REGEXP_LIKE(persona_key, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  display_name      VARCHAR(200) NOT NULL,
  ocean             JSON NOT NULL,  -- {O,C,E,A,N} 0–100 (rudi_platform.persona.ocean)
  voice             JSON NOT NULL DEFAULT (JSON_OBJECT()),
  languages         JSON NOT NULL DEFAULT (JSON_ARRAY('en')) CHECK (JSON_TYPE(languages) = 'ARRAY'),
  briefing          TEXT NOT NULL DEFAULT (''),
  PRIMARY KEY (id),
  UNIQUE KEY personas_key_uq (tenant_id, clone_version_id, persona_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- -------------------------------------------------------------------------------------- skills
-- Shared inventory: every tenant may read it, so no tenant_id.
CREATE TABLE registry_skills (
  id            CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^skl_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  slug          VARCHAR(63) NOT NULL CHECK (REGEXP_LIKE(slug, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  display_name  VARCHAR(200) NOT NULL,
  kind          VARCHAR(16) NOT NULL CHECK (kind IN ('real_time', 'turn', 'background')),
  created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY skills_slug_uq (slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE registry_skill_versions (
  id           CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^skv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  skill_id     CHAR(36) NOT NULL
               CHECK (REGEXP_LIKE(skill_id, '^skl_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  major        INT NOT NULL CHECK (major >= 0),
  minor        INT NOT NULL CHECK (minor >= 0),
  patch        INT NOT NULL CHECK (patch >= 0),
  status       VARCHAR(16) NOT NULL DEFAULT 'candidate'
               CHECK (status IN ('candidate', 'released', 'retired')),
  manifest     JSON NOT NULL,
  brief_path   VARCHAR(500),  -- the rudi-skill-brief@1 file, e.g. evals/skill-briefs/goal-setting.skill.md
  gate_run_id  CHAR(36) CHECK (REGEXP_LIKE(gate_run_id, '^gat_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  released_at  DATETIME(6),
  PRIMARY KEY (id),
  UNIQUE KEY skill_versions_semver_uq (skill_id, major, minor, patch),
  -- No release without a passing arena gate (decision #25, blueprint §5).
  CHECK (status <> 'released' OR (released_at IS NOT NULL AND gate_run_id IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE registry_skill_packs (
  id          CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^pak_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  slug        VARCHAR(63) NOT NULL CHECK (REGEXP_LIKE(slug, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  major       INT NOT NULL CHECK (major >= 0),
  minor       INT NOT NULL CHECK (minor >= 0),
  patch       INT NOT NULL CHECK (patch >= 0),
  contents    JSON NOT NULL,  -- [{skill, major, defaults}]
  created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY skill_packs_semver_uq (slug, major, minor, patch)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- ------------------------------------------------------------------ endpoints, keys, webhooks
-- An inbound message names only the business address it reached, so the router finds the tenant
-- through endpoints_address_uq: one of the closed list of resolvers (decision #47).
CREATE TABLE registry_endpoints (
  id          CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^enp_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id   CHAR(36) NOT NULL
              CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id    CHAR(36) NOT NULL CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  channel     VARCHAR(16) NOT NULL
              CHECK (channel IN ('whatsapp', 'voice', 'api', 'widget', 'sms', 'email', 'telegram')),
  address     VARCHAR(255) NOT NULL,  -- the business side: sender number, caller id, widget key id
  status      VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY endpoints_address_uq (channel, address),  -- one address routes to exactly one clone
  KEY endpoints_clone_idx (tenant_id, clone_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE registry_api_keys (
  id              CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^key_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id       CHAR(36) NOT NULL
                  CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id        CHAR(36) NOT NULL
                  CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  stage           VARCHAR(16) NOT NULL CHECK (stage IN ('dev', 'prod')),
  key_hash        VARBINARY(32) NOT NULL CHECK (LENGTH(key_hash) = 32),  -- SHA-256; key shown once
  display_prefix  VARCHAR(16) NOT NULL,  -- first characters, to tell keys apart
  created_by      CHAR(36) CHECK (REGEXP_LIKE(created_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  revoked_at      DATETIME(6),
  PRIMARY KEY (id),
  UNIQUE KEY api_keys_hash_uq (key_hash),
  KEY api_keys_clone_idx (tenant_id, clone_id, revoked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE registry_webhooks (
  id           CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^whk_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id    CHAR(36) NOT NULL
               CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id     CHAR(36) NOT NULL CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  url          VARCHAR(2048) NOT NULL CHECK (REGEXP_LIKE(url, '^https://', 'c')),
  secret_name  VARCHAR(512) NOT NULL,  -- Secrets Manager name of the signing secret
  events       JSON NOT NULL CHECK (JSON_TYPE(events) = 'ARRAY'),
  status       VARCHAR(16) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY webhooks_clone_idx (tenant_id, clone_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- ------------------------------------------------------------- scope requests and minting (§5.2)
CREATE TABLE registry_scope_requests (
  id            CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^scp_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id     CHAR(36) NOT NULL
                CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id      CHAR(36) CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  capabilities  JSON NOT NULL,  -- [{id, text}], one line per capability
  vetting       VARCHAR(16) NOT NULL DEFAULT 'pending'
                CHECK (vetting IN ('pending', 'approved', 'narrowed', 'rejected')),
  vetting_note  TEXT NOT NULL DEFAULT (''),
  vetted_by     CHAR(36) CHECK (REGEXP_LIKE(vetted_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  coverage      JSON,  -- per-capability coverage and the overall percentage
  created_by    CHAR(36) CHECK (REGEXP_LIKE(created_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY scope_requests_tenant_idx (tenant_id, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- A tenant follows its own mint requests; only the platform writes them.
CREATE TABLE registry_mint_requests (
  id                CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^mnt_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id         CHAR(36) NOT NULL  -- who asked
                    CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  scope_request_id  CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(scope_request_id, '^scp_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  capability        VARCHAR(1000) NOT NULL,
  skill_slug        VARCHAR(63) CHECK (REGEXP_LIKE(skill_slug, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  status            VARCHAR(16) NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'briefed', 'in_rounds', 'released', 'abandoned')),
  rounds            INT NOT NULL DEFAULT 0 CHECK (rounds >= 0),
  skill_version_id  CHAR(36)
                    CHECK (REGEXP_LIKE(skill_version_id, '^skv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY mint_requests_scope_idx (tenant_id, scope_request_id),
  CHECK (status <> 'released' OR skill_version_id IS NOT NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- -------------------------------------------------------------------------------- audit_trail
-- Ids and decisions only, never names or content. Append-only: the registry store only ever
-- inserts. tenant_id NULL is a platform event; the trail outlives an offboarded tenant.
CREATE TABLE audit_trail_events (
  id                CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^aud_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  occurred_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  actor_account_id  CHAR(36)
                    CHECK (REGEXP_LIKE(actor_account_id, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id         CHAR(36) CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  action_key        VARCHAR(64) NOT NULL,  -- e.g. role.assign, clone.publish, breakglass.open
  target_id         CHAR(36) CHECK (REGEXP_LIKE(target_id, '^[a-z]{3}_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  decision          JSON NOT NULL DEFAULT (JSON_OBJECT()),
  PRIMARY KEY (id),
  KEY events_tenant_idx (tenant_id, occurred_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
