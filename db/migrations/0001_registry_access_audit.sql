-- MEET_RUDI platform — 0001: registry, access and audit schemas.
--
-- Target: PlanetScale Postgres, eu-central-1, Postgres 17+ (decision #30).
-- Never edit a migration that has run anywhere; add the next number instead.
--
-- Ids: every id column holds `{cat}_` + 32 lowercase Crockford base32 characters
-- (docs/platform/conventions.md §1). The application mints them with rudi_platform.ids;
-- public.rudi_new_id() exists for migrations and seeds only.
--
-- Tenant isolation (blueprint §6): every table with a tenant_id has row-level security, forced
-- for the table owner too. A request sets, inside its transaction,
--     SET LOCAL rudi.tenant_id = 'ten_…'     -- tenant-scoped work
--     SET LOCAL rudi.platform  = 'on'        -- platform work (console admin, migrations, jobs)
-- A query that forgets both sees nothing. The application role must not have BYPASSRLS.

CREATE SCHEMA IF NOT EXISTS registry;
CREATE SCHEMA IF NOT EXISTS access;
CREATE SCHEMA IF NOT EXISTS audit;

-- --------------------------------------------------------------------------------- id helpers
CREATE DOMAIN public.rudi_id AS text
  CHECK (VALUE ~ '^[a-z]{3}_[0-9a-hjkmnp-tv-z]{32}$');

-- 160 bits as 32 Crockford base32 symbols. gen_random_uuid() is core Postgres; two of them give
-- 32 random-ish bytes (each v4 UUID fixes 6 bits), of which the first 20 are used.
CREATE FUNCTION public.rudi_new_id(prefix text) RETURNS text
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
  alphabet constant text := '0123456789abcdefghjkmnpqrstvwxyz';
  raw bytea := decode(replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''), 'hex');
  body text := '';
  acc bigint := 0;
  bits integer := 0;
  i integer;
BEGIN
  IF prefix !~ '^[a-z]{3}$' THEN
    RAISE EXCEPTION 'id prefix must be three lowercase letters, got %', prefix;
  END IF;
  FOR i IN 0..19 LOOP
    acc := (acc << 8) | get_byte(raw, i);
    bits := bits + 8;
    WHILE bits >= 5 LOOP
      bits := bits - 5;
      body := body || substr(alphabet, ((acc >> bits) & 31)::integer + 1, 1);
    END LOOP;
    acc := acc & ((1::bigint << bits) - 1);
  END LOOP;
  RETURN prefix || '_' || body;
END
$$;

CREATE FUNCTION public.rudi_is_platform() RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce(current_setting('rudi.platform', true), '') = 'on'
$$;

CREATE FUNCTION public.rudi_tenant() RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting('rudi.tenant_id', true), '')
$$;

-- ------------------------------------------------------------------------------------- cells
CREATE TABLE registry.cells (
  id          public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'cel_'),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^(pool|d-[a-z0-9]{1,10})$'),
  kind        text NOT NULL CHECK (kind IN ('pooled', 'dedicated')),
  region      text NOT NULL DEFAULT 'eu-central-1',
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------------- tenants
CREATE TABLE registry.tenants (
  id              public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'ten_'),
  slug            text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  name            text NOT NULL,
  cell_id         public.rudi_id NOT NULL REFERENCES registry.cells (id),
  status          text NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended', 'offboarding')),
  acceptable_use  jsonb NOT NULL DEFAULT '{}'::jsonb,  -- exclusions profile (decision #27)
  model_policy    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- gateway ModelPolicy (blueprint §10)
  limits          jsonb NOT NULL DEFAULT '{}'::jsonb,  -- rate limits, spend alerts, kill switch
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------------------------ access
CREATE TABLE access.accounts (
  id             public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'acc_'),
  email          text NOT NULL UNIQUE CHECK (email = lower(email) AND position('@' IN email) > 1),
  display_name   text NOT NULL DEFAULT '',
  password_hash  text,  -- PBKDF2-SHA256, as the arena; NULL for SSO-only accounts later
  status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'locked', 'disabled')),
  failed_logins  integer NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_login_at  timestamptz
);

CREATE TABLE access.permissions (
  id           public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'prm_'),
  key          text NOT NULL UNIQUE CHECK (key ~ '^[a-z]+(\.[a-z_]+)+$'),
  scope        text NOT NULL CHECK (scope IN ('platform', 'tenant')),
  description  text NOT NULL
);

CREATE TABLE access.roles (
  id                          public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'rol_'),
  key                         text NOT NULL UNIQUE CHECK (key ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  name                        text NOT NULL,
  scope                       text NOT NULL CHECK (scope IN ('platform', 'tenant')),
  assignable_by_tenant_admin  boolean NOT NULL DEFAULT false,
  seed                        boolean NOT NULL DEFAULT false,
  created_by                  public.rudi_id REFERENCES access.accounts (id),
  created_at                  timestamptz NOT NULL DEFAULT now(),
  CHECK (scope = 'tenant' OR NOT assignable_by_tenant_admin)
);

CREATE TABLE access.role_permissions (
  role_id        public.rudi_id NOT NULL REFERENCES access.roles (id) ON DELETE CASCADE,
  permission_id  public.rudi_id NOT NULL REFERENCES access.permissions (id),
  PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE access.role_assignments (
  id          public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'rga_'),
  account_id  public.rudi_id NOT NULL REFERENCES access.accounts (id) ON DELETE CASCADE,
  role_id     public.rudi_id NOT NULL REFERENCES access.roles (id),
  tenant_id   public.rudi_id REFERENCES registry.tenants (id),  -- NULL = platform-wide
  granted_by  public.rudi_id REFERENCES access.accounts (id),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (account_id, role_id, tenant_id)
);

-- A platform role is assigned platform-wide; a tenant role always in exactly one tenant.
CREATE FUNCTION access.check_assignment_scope() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  role_scope text;
BEGIN
  SELECT scope INTO role_scope FROM access.roles WHERE id = NEW.role_id;
  IF role_scope = 'platform' AND NEW.tenant_id IS NOT NULL THEN
    RAISE EXCEPTION 'platform role % cannot be assigned in a tenant', NEW.role_id;
  END IF;
  IF role_scope = 'tenant' AND NEW.tenant_id IS NULL THEN
    RAISE EXCEPTION 'tenant role % needs a tenant', NEW.role_id;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER role_assignments_scope
  BEFORE INSERT OR UPDATE ON access.role_assignments
  FOR EACH ROW EXECUTE FUNCTION access.check_assignment_scope();

CREATE TABLE access.sessions (
  id            public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'ses_'),
  account_id    public.rudi_id NOT NULL REFERENCES access.accounts (id) ON DELETE CASCADE,
  token_hash    bytea NOT NULL UNIQUE,  -- SHA-256 of the session token, never the token
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL
);

CREATE TABLE access.invitations (
  id           public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'inv_'),
  tenant_id    public.rudi_id NOT NULL REFERENCES registry.tenants (id),
  email        text NOT NULL CHECK (email = lower(email)),
  role_id      public.rudi_id NOT NULL REFERENCES access.roles (id),
  token_hash   bytea NOT NULL UNIQUE,
  invited_by   public.rudi_id REFERENCES access.accounts (id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  accepted_at  timestamptz
);

-- ------------------------------------------------------------------------- clones and personas
CREATE TABLE registry.clones (
  id               public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'cln_'),
  tenant_id        public.rudi_id NOT NULL REFERENCES registry.tenants (id),
  slug             text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  name             text NOT NULL,
  purpose          text NOT NULL CHECK (length(purpose) BETWEEN 10 AND 500),  -- declared (T2)
  status           text NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'live', 'paused', 'retired')),
  live_version_id  public.rudi_id,  -- FK added below, once clone_versions exists
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, slug)
);

CREATE TABLE registry.clone_versions (
  id            public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'cvr_'),
  tenant_id     public.rudi_id NOT NULL REFERENCES registry.tenants (id),
  clone_id      public.rudi_id NOT NULL REFERENCES registry.clones (id),
  number        integer NOT NULL CHECK (number > 0),
  stage         text NOT NULL DEFAULT 'draft'
                CHECK (stage IN ('draft', 'staging', 'live', 'retired')),
  composition   jsonb NOT NULL DEFAULT '{}'::jsonb,  -- skill@major pins and overrides
  journey       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- stage graph
  settings      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- quiet hours, cadence, retention, channels
  created_by    public.rudi_id REFERENCES access.accounts (id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz,
  UNIQUE (clone_id, number)
);

ALTER TABLE registry.clones
  ADD CONSTRAINT clones_live_version_fk
  FOREIGN KEY (live_version_id) REFERENCES registry.clone_versions (id);

CREATE TABLE registry.personas (
  id                public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'per_'),
  tenant_id         public.rudi_id NOT NULL REFERENCES registry.tenants (id),
  clone_version_id  public.rudi_id NOT NULL REFERENCES registry.clone_versions (id),
  key               text NOT NULL CHECK (key ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),  -- stable; users pin it
  name              text NOT NULL,
  ocean             jsonb NOT NULL,  -- {O,C,E,A,N} 0–100 (rudi_platform.persona.ocean)
  voice             jsonb NOT NULL DEFAULT '{}'::jsonb,
  languages         text[] NOT NULL DEFAULT ARRAY['en'],
  briefing          text NOT NULL DEFAULT '',
  UNIQUE (clone_version_id, key)
);

-- Published clone versions are immutable, and so are their personas (blueprint §4.2).
CREATE FUNCTION registry.freeze_published_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.stage <> 'draft' AND (
       NEW.composition IS DISTINCT FROM OLD.composition
    OR NEW.journey IS DISTINCT FROM OLD.journey
    OR NEW.settings IS DISTINCT FROM OLD.settings
    OR NEW.clone_id IS DISTINCT FROM OLD.clone_id
    OR NEW.number IS DISTINCT FROM OLD.number) THEN
    RAISE EXCEPTION 'clone version % is published and immutable', OLD.id;
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER clone_versions_freeze
  BEFORE UPDATE ON registry.clone_versions
  FOR EACH ROW EXECUTE FUNCTION registry.freeze_published_version();

CREATE FUNCTION registry.freeze_published_personas() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  version_stage text;
BEGIN
  SELECT stage INTO version_stage FROM registry.clone_versions
   WHERE id = coalesce(NEW.clone_version_id, OLD.clone_version_id);
  IF version_stage <> 'draft' THEN
    RAISE EXCEPTION 'personas of a published clone version are immutable';
  END IF;
  RETURN coalesce(NEW, OLD);
END
$$;

CREATE TRIGGER personas_freeze
  BEFORE INSERT OR UPDATE OR DELETE ON registry.personas
  FOR EACH ROW EXECUTE FUNCTION registry.freeze_published_personas();

-- -------------------------------------------------------------------------------------- skills
-- Shared inventory: every tenant may read it, so no tenant_id and no row-level security.
CREATE TABLE registry.skills (
  id          public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'skl_'),
  slug        text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  name        text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('real_time', 'turn', 'background')),
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE registry.skill_versions (
  id           public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'skv_'),
  skill_id     public.rudi_id NOT NULL REFERENCES registry.skills (id),
  major        integer NOT NULL CHECK (major >= 0),
  minor        integer NOT NULL CHECK (minor >= 0),
  patch        integer NOT NULL CHECK (patch >= 0),
  status       text NOT NULL DEFAULT 'candidate'
               CHECK (status IN ('candidate', 'released', 'retired')),
  manifest     jsonb NOT NULL,
  brief_path   text,  -- the rudi-skill-brief@1 file, e.g. evals/skill-briefs/goal-setting.skill.md
  gate_run_id  public.rudi_id CHECK (gate_run_id IS NULL OR left(gate_run_id, 4) = 'gat_'),
  created_at   timestamptz NOT NULL DEFAULT now(),
  released_at  timestamptz,
  UNIQUE (skill_id, major, minor, patch),
  -- No release without a passing arena gate (decision #25, blueprint §5).
  CHECK (status <> 'released' OR (released_at IS NOT NULL AND gate_run_id IS NOT NULL))
);

CREATE TABLE registry.skill_packs (
  id          public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'pak_'),
  slug        text NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  major       integer NOT NULL CHECK (major >= 0),
  minor       integer NOT NULL CHECK (minor >= 0),
  patch       integer NOT NULL CHECK (patch >= 0),
  contents    jsonb NOT NULL,  -- [{skill, major, defaults}]
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (slug, major, minor, patch)
);

-- ------------------------------------------------------------------ endpoints, keys, webhooks
CREATE TABLE registry.endpoints (
  id          public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'enp_'),
  tenant_id   public.rudi_id NOT NULL REFERENCES registry.tenants (id),
  clone_id    public.rudi_id NOT NULL REFERENCES registry.clones (id),
  channel     text NOT NULL
              CHECK (channel IN ('whatsapp', 'voice', 'api', 'widget', 'sms', 'email', 'telegram')),
  address     text NOT NULL,  -- the business side: sender number, caller id, widget key id
  status      text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, address)   -- one address routes to exactly one clone
);

CREATE TABLE registry.api_keys (
  id              public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'key_'),
  tenant_id       public.rudi_id NOT NULL REFERENCES registry.tenants (id),
  clone_id        public.rudi_id NOT NULL REFERENCES registry.clones (id),
  stage           text NOT NULL CHECK (stage IN ('dev', 'prod')),
  key_hash        bytea NOT NULL UNIQUE,  -- SHA-256; the key itself is shown once
  display_prefix  text NOT NULL,          -- first characters, to tell keys apart
  created_by      public.rudi_id REFERENCES access.accounts (id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  revoked_at      timestamptz
);

CREATE TABLE registry.webhooks (
  id           public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'whk_'),
  tenant_id    public.rudi_id NOT NULL REFERENCES registry.tenants (id),
  clone_id     public.rudi_id NOT NULL REFERENCES registry.clones (id),
  url          text NOT NULL CHECK (url ~ '^https://'),
  secret_name  text NOT NULL,  -- Secrets Manager name of the signing secret
  events       text[] NOT NULL,
  status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------- scope requests and minting (§5.2)
CREATE TABLE registry.scope_requests (
  id            public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'scp_'),
  tenant_id     public.rudi_id NOT NULL REFERENCES registry.tenants (id),
  clone_id      public.rudi_id REFERENCES registry.clones (id),
  capabilities  jsonb NOT NULL,  -- [{id, text}], one line per capability
  vetting       text NOT NULL DEFAULT 'pending'
                CHECK (vetting IN ('pending', 'approved', 'narrowed', 'rejected')),
  vetting_note  text NOT NULL DEFAULT '',
  vetted_by     public.rudi_id REFERENCES access.accounts (id),
  match         jsonb,           -- per-capability coverage and the overall percentage
  created_by    public.rudi_id REFERENCES access.accounts (id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE registry.mint_requests (
  id                public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'mnt_'),
  tenant_id         public.rudi_id NOT NULL REFERENCES registry.tenants (id),  -- who asked
  scope_request_id  public.rudi_id NOT NULL REFERENCES registry.scope_requests (id),
  capability        text NOT NULL,
  skill_slug        text CHECK (skill_slug IS NULL OR skill_slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  status            text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'briefed', 'in_rounds', 'released', 'abandoned')),
  rounds            integer NOT NULL DEFAULT 0 CHECK (rounds >= 0),
  skill_version_id  public.rudi_id REFERENCES registry.skill_versions (id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (status <> 'released' OR skill_version_id IS NOT NULL)
);

-- -------------------------------------------------------------------------------------- audit
-- Ids and decisions only, never names or content. Append-only.
CREATE TABLE audit.events (
  id                public.rudi_id PRIMARY KEY CHECK (left(id, 4) = 'aud_'),
  at                timestamptz NOT NULL DEFAULT now(),
  actor_account_id  public.rudi_id REFERENCES access.accounts (id),
  tenant_id         public.rudi_id,  -- no foreign key: the trail outlives an offboarded tenant
  action            text NOT NULL,   -- e.g. role.assign, clone.publish, breakglass.open
  target_id         public.rudi_id,
  decision          jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE FUNCTION audit.refuse_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit events are append-only';
END
$$;

CREATE TRIGGER events_append_only
  BEFORE UPDATE OR DELETE ON audit.events
  FOR EACH ROW EXECUTE FUNCTION audit.refuse_change();

-- ------------------------------------------------------------------------------------ indexes
CREATE INDEX clones_tenant_idx ON registry.clones (tenant_id);
CREATE INDEX clone_versions_clone_idx ON registry.clone_versions (clone_id, number DESC);
CREATE INDEX personas_version_idx ON registry.personas (clone_version_id);
CREATE INDEX endpoints_clone_idx ON registry.endpoints (clone_id);
CREATE INDEX api_keys_clone_idx ON registry.api_keys (clone_id) WHERE revoked_at IS NULL;
CREATE INDEX webhooks_clone_idx ON registry.webhooks (clone_id);
CREATE INDEX scope_requests_tenant_idx ON registry.scope_requests (tenant_id, created_at DESC);
CREATE INDEX mint_requests_scope_idx ON registry.mint_requests (scope_request_id);
CREATE INDEX role_assignments_account_idx ON access.role_assignments (account_id);
CREATE INDEX role_assignments_tenant_idx ON access.role_assignments (tenant_id);
CREATE INDEX invitations_tenant_idx ON access.invitations (tenant_id);
CREATE INDEX sessions_account_idx ON access.sessions (account_id);
CREATE INDEX audit_events_tenant_idx ON audit.events (tenant_id, at DESC);

-- -------------------------------------------------------------------------- row-level security
-- A tenant sees its own row; the platform sees all.
ALTER TABLE registry.tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.tenants
  USING (public.rudi_is_platform() OR id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform());

ALTER TABLE registry.clones ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.clones FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.clones
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

ALTER TABLE registry.clone_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.clone_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.clone_versions
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

ALTER TABLE registry.personas ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.personas FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.personas
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

ALTER TABLE registry.endpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.endpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.endpoints
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

ALTER TABLE registry.api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.api_keys FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.api_keys
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

ALTER TABLE registry.webhooks ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.webhooks FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.webhooks
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

ALTER TABLE registry.scope_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.scope_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.scope_requests
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

-- A tenant follows its own mint requests; only the platform writes them.
ALTER TABLE registry.mint_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.mint_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON registry.mint_requests
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform());

ALTER TABLE access.invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE access.invitations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON access.invitations
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

-- Platform-wide assignments (tenant_id NULL) are visible to the platform only.
ALTER TABLE access.role_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE access.role_assignments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON access.role_assignments
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());

ALTER TABLE audit.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit.events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON audit.events
  USING (public.rudi_is_platform() OR tenant_id = public.rudi_tenant())
  WITH CHECK (public.rudi_is_platform() OR tenant_id = public.rudi_tenant());
