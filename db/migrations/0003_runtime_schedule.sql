-- MEET_RUDI platform — 0003: end-users, conversations, turns, messages, schedule.
--
-- Target: PlanetScale Vitess (MySQL 8), eu-central-1 (decision #46). Never edit a migration that
-- has run anywhere; add the next number instead. Design: docs/platform/blueprint.md §2, §6, §7
-- and decision #42.
--
-- What is personal stays encrypted. Message text, channel handles (phone numbers, addresses) and
-- the engine's conversation state are stored only as AWS Encryption SDK messages under the
-- tenant's KMS key (blueprint §6), in *_enc columns whose check refuses anything else: such a
-- message starts with its format version, 1 or 2, while plain text (JSON starts with '{', words
-- with a letter) never does. Everything stored in the clear is an id, a label, a number or a
-- time. S3 keys in columns (*_s3_key) sit in the tenant's folder, tenants/{slug}/ (decision #41).
--
-- Isolation as in 0001: every table has its own tenant_id, every statement names it (decision
-- #47) and every index leads with it. No foreign keys: the stores delete an end-user's rows table
-- by table (erasure, blueprint §7.3).

-- ---------------------------------------------------------------------------------- end-users
-- An end-user id is derived from the tenant salt and a channel handle (conventions §1): per clone
-- by default, tenant-wide when the tenant links identities (clone_id NULL).
CREATE TABLE runtime_end_users (
  id            CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^usr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id     CHAR(36) NOT NULL
                CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id      CHAR(36) CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  locale        VARCHAR(35) CHECK (REGEXP_LIKE(locale, '^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$', 'c')),
  time_zone     VARCHAR(64),  -- IANA name, e.g. Europe/Brussels
  status        VARCHAR(16) NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'stopped', 'blocked')),
  created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  last_seen_at  DATETIME(6),
  PRIMARY KEY (id),
  KEY end_users_tenant_idx (tenant_id, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- A channel handle is contact data: kept as ciphertext so messages can be delivered, found by a
-- keyed hash (HMAC-SHA256 with the tenant salt), never stored in the clear. The same handle maps
-- to one end-user per clone, so the hash repeats across a tenant's clones.
CREATE TABLE runtime_handles (
  id           CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^hdl_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id    CHAR(36) NOT NULL
               CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  end_user_id  CHAR(36) NOT NULL
               CHECK (REGEXP_LIKE(end_user_id, '^usr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  channel      VARCHAR(16) NOT NULL
               CHECK (channel IN ('whatsapp', 'voice', 'api', 'widget', 'sms', 'email', 'telegram')),
  lookup_hash  VARBINARY(32) NOT NULL CHECK (LENGTH(lookup_hash) = 32),
  value_enc    BLOB NOT NULL CHECK (LENGTH(value_enc) > 1 AND ASCII(value_enc) IN (1, 2)),
  verified_at  DATETIME(6),
  created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY handles_user_uq (tenant_id, end_user_id, channel, lookup_hash),
  KEY handles_lookup_idx (tenant_id, channel, lookup_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- A tenant-approved link between two end-user ids; one row per pair, smaller id first.
CREATE TABLE runtime_identity_links (
  id              CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^lnk_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id       CHAR(36) NOT NULL
                  CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  end_user_id     CHAR(36) NOT NULL
                  CHECK (REGEXP_LIKE(end_user_id, '^usr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  linked_user_id  CHAR(36) NOT NULL
                  CHECK (REGEXP_LIKE(linked_user_id, '^usr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  approved_by     CHAR(36) CHECK (REGEXP_LIKE(approved_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY identity_links_pair_uq (tenant_id, end_user_id, linked_user_id),
  KEY identity_links_linked_idx (tenant_id, linked_user_id),
  CHECK (end_user_id < linked_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- Consent gates processing (CLAUDE.md §5). Append-only by use: a withdrawal is a new row, and the
-- latest row per end-user, clone and purpose is the current state.
CREATE TABLE runtime_consents (
  id               CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^cns_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id        CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  end_user_id      CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(end_user_id, '^usr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id         CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  purpose          VARCHAR(64) NOT NULL  -- e.g. health-data
                   CHECK (REGEXP_LIKE(purpose, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  is_granted       BOOLEAN NOT NULL,
  recorded_via     VARCHAR(16) NOT NULL
                   CHECK (recorded_via IN ('form', 'chat', 'api', 'operator', 'migration')),
  form_version     VARCHAR(32),
  evidence_s3_key  VARCHAR(1024)  -- the signed record, if there is one
                   CHECK (REGEXP_LIKE(evidence_s3_key, '^tenants/[a-z0-9]+(-[a-z0-9]+)*/[A-Za-z0-9]', 'c')),
  recorded_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY consents_current_idx (tenant_id, end_user_id, clone_id, purpose, recorded_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- ------------------------------------------------------------------------------ conversations
-- An end-user's relationship with one clone on one channel (blueprint §2; the memory schema is
-- per channel, conventions §4). The engine's state (rudi_platform.memory.ConversationState in its
-- flat form: stage, session, history, memory fields) is stored whole as ciphertext. The columns
-- beside it are what routing, the 24-hour window and the console need without decrypting.
CREATE TABLE runtime_conversations (
  id                CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^cnv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id         CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  end_user_id       CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(end_user_id, '^usr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_id          CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(clone_id, '^cln_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  channel           VARCHAR(16) NOT NULL
                    CHECK (channel IN ('whatsapp', 'voice', 'api', 'widget', 'sms', 'email', 'telegram')),
  clone_version_id  CHAR(36)  -- last served by
                    CHECK (REGEXP_LIKE(clone_version_id, '^cvr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  persona_key       VARCHAR(64) CHECK (REGEXP_LIKE(persona_key, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  stage             VARCHAR(64),  -- journey stage; NULL = never spoken, 'concluded' = session over
  session_number    INT NOT NULL DEFAULT 0 CHECK (session_number >= 0),
  state_enc         MEDIUMBLOB CHECK (LENGTH(state_enc) > 1 AND ASCII(state_enc) IN (1, 2)),
  state_rev         INT NOT NULL DEFAULT 0 CHECK (state_rev >= 0),  -- optimistic writes
  status            VARCHAR(16) NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'halted', 'taken_over', 'closed')),
  last_inbound_at   DATETIME(6),  -- the 24-hour window runs from here (CLAUDE.md §3)
  last_outbound_at  DATETIME(6),
  created_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  UNIQUE KEY conversations_user_uq (tenant_id, end_user_id, clone_id, channel),
  KEY conversations_tenant_idx (tenant_id, updated_at DESC),
  KEY conversations_clone_idx (tenant_id, clone_id, updated_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- One inbound-to-reply cycle, one call turn or one proactive send. It records the exact skill
-- versions that ran (blueprint §4.2). Labels and ids only: the words are in runtime_messages,
-- and the envelope's signals (which can carry what the person said) are not kept here.
CREATE TABLE runtime_turns (
  id                CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^trn_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id         CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  conversation_id   CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(conversation_id, '^cnv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  clone_version_id  CHAR(36) NOT NULL
                    CHECK (REGEXP_LIKE(clone_version_id, '^cvr_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  kind              VARCHAR(16) NOT NULL
                    CHECK (kind IN ('inbound', 'proactive', 'call_opening', 'call_turn')),
  engine            VARCHAR(16) NOT NULL DEFAULT 'platform' CHECK (engine IN ('platform', 'legacy')),
  skills            JSON NOT NULL DEFAULT (JSON_ARRAY())  -- slug@x.y.z of every skill that ran
                    CHECK (JSON_TYPE(skills) = 'ARRAY'),
  stage_before      VARCHAR(64),
  stage_after       VARCHAR(64),
  outcome           VARCHAR(16) NOT NULL
                    CHECK (outcome IN ('replied', 'blocked', 'failed', 'halted', 'skipped')),
  guard             JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- why a reply was replaced: labels only
  latency_ms        INT CHECK (latency_ms >= 0),
  started_at        DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  finished_at       DATETIME(6),
  PRIMARY KEY (id),
  KEY turns_conversation_idx (tenant_id, conversation_id, started_at DESC),
  KEY turns_tenant_idx (tenant_id, started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- One stored message. Recent messages live here; older ones are archived to S3 under the
-- tenant's folder by the clone's retention (blueprint §7.1, §7.3). A delivery callback finds its
-- message by provider_ref within the tenant its endpoint resolved to.
CREATE TABLE runtime_messages (
  id               CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^msg_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id        CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  conversation_id  CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(conversation_id, '^cnv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  turn_id          CHAR(36) CHECK (REGEXP_LIKE(turn_id, '^trn_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  direction        VARCHAR(8) NOT NULL CHECK (direction IN ('in', 'out')),
  author           VARCHAR(16) NOT NULL CHECK (author IN ('end_user', 'clone', 'operator', 'system')),
  body_enc         MEDIUMBLOB NOT NULL CHECK (LENGTH(body_enc) > 1 AND ASCII(body_enc) IN (1, 2)),
  media            JSON NOT NULL DEFAULT (JSON_ARRAY())  -- [{kind, s3_key}], keys under tenants/{slug}/
                   CHECK (JSON_TYPE(media) = 'ARRAY'),
  template_key     VARCHAR(128),  -- the pre-approved template of an out-of-window send
  provider_ref     VARCHAR(128),  -- the channel's message id, e.g. a Twilio SID
  delivery         VARCHAR(16) NOT NULL DEFAULT 'stored'
                   CHECK (delivery IN ('stored', 'queued', 'sent', 'delivered', 'read', 'failed')),
  created_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY messages_conversation_idx (tenant_id, conversation_id, created_at DESC),
  KEY messages_turn_idx (tenant_id, turn_id),
  KEY messages_provider_idx (tenant_id, provider_ref),
  CHECK ((direction = 'in') = (author = 'end_user'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- A red-flag event routed to a tenant contact (crisis, objection, medical-advice request…).
CREATE TABLE runtime_escalations (
  id               CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^esc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id        CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  conversation_id  CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(conversation_id, '^cnv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  turn_id          CHAR(36) CHECK (REGEXP_LIKE(turn_id, '^trn_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  kind             VARCHAR(64) NOT NULL CHECK (REGEXP_LIKE(kind, '^[a-z0-9]+(-[a-z0-9]+)*$', 'c')),
  detected_by      VARCHAR(16) NOT NULL CHECK (detected_by IN ('lexicon', 'model', 'operator')),
  status           VARCHAR(16) NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'acknowledged', 'closed')),
  acknowledged_by  CHAR(36)
                   CHECK (REGEXP_LIKE(acknowledged_by, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  created_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  closed_at        DATETIME(6),
  PRIMARY KEY (id),
  KEY escalations_conversation_idx (tenant_id, conversation_id),
  KEY escalations_turn_idx (tenant_id, turn_id),
  KEY escalations_open_idx (tenant_id, status, created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- An operator taking over a conversation. At most one open takeover per conversation:
-- open_conversation_id is the conversation while the takeover is open and NULL once it ended, and
-- a unique key lets NULLs repeat.
CREATE TABLE runtime_takeovers (
  id                    CHAR(36) NOT NULL
                        CHECK (REGEXP_LIKE(id, '^tko_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id             CHAR(36) NOT NULL
                        CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  conversation_id       CHAR(36) NOT NULL
                        CHECK (REGEXP_LIKE(conversation_id, '^cnv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  account_id            CHAR(36) NOT NULL
                        CHECK (REGEXP_LIKE(account_id, '^acc_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  started_at            DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  ended_at              DATETIME(6),
  open_conversation_id  CHAR(36) GENERATED ALWAYS AS (IF(ended_at IS NULL, conversation_id, NULL)) STORED,
  PRIMARY KEY (id),
  UNIQUE KEY takeovers_one_open_uq (tenant_id, open_conversation_id),
  KEY takeovers_conversation_idx (tenant_id, conversation_id),
  CHECK (ended_at IS NULL OR ended_at >= started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

-- ------------------------------------------------------------------------------------ schedule
-- A due item: check-in, reach-out, retention or a background skill run. Each minute the scheduler
-- claims due rows tenant by tenant (decision #47) with SELECT … FOR UPDATE SKIP LOCKED and emits
-- schedule.due (blueprint §3). Quiet hours and the window rule are applied when the item fires,
-- in the end-user's time zone.
CREATE TABLE schedule_items (
  id               CHAR(36) NOT NULL CHECK (REGEXP_LIKE(id, '^sch_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  tenant_id        CHAR(36) NOT NULL
                   CHECK (REGEXP_LIKE(tenant_id, '^ten_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  conversation_id  CHAR(36)
                   CHECK (REGEXP_LIKE(conversation_id, '^cnv_[0-9a-hjkmnp-tv-z]{32}$', 'c')),
  kind             VARCHAR(16) NOT NULL
                   CHECK (kind IN ('check_in', 'reach_out', 'retention', 'skill')),
  skill            VARCHAR(128)
                   CHECK (REGEXP_LIKE(skill, '^[a-z0-9]+(-[a-z0-9]+)*(@[0-9]+([.][0-9]+){0,2})?$', 'c')),
  due_at           DATETIME(6) NOT NULL,
  status           VARCHAR(16) NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'claimed', 'done', 'cancelled', 'failed')),
  attempts         INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  payload          JSON NOT NULL DEFAULT (JSON_OBJECT()),  -- ids and settings only, never content
  last_error       VARCHAR(128),  -- an error kind, never content
  claimed_at       DATETIME(6),
  done_at          DATETIME(6),
  created_at       DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),
  KEY items_due_idx (tenant_id, status, due_at),
  KEY items_tenant_idx (tenant_id, due_at),
  KEY items_conversation_idx (tenant_id, conversation_id),
  CHECK (kind <> 'skill' OR skill IS NOT NULL)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
