CREATE TABLE IF NOT EXISTS users (
    user_pubkey TEXT PRIMARY KEY CHECK (length(user_pubkey) = 64),
    created_at  TIMESTAMPTZ DEFAULT now(),
    disabled_at TIMESTAMPTZ,
    enrollment_state TEXT NOT NULL DEFAULT 'active',
    enrollment_requested_at TIMESTAMPTZ,
    enrollment_approved_at TIMESTAMPTZ,
    plan TEXT NOT NULL DEFAULT 'default',
    max_frames_per_hour INT,
    max_storage_mb INT,
    wrapped_data_key BYTEA,
    data_key_source TEXT NOT NULL DEFAULT 'server_wrapped',
    client_key_last_seen_at TIMESTAMPTZ,
    data_key_created_at TIMESTAMPTZ,
    data_key_rotated_at TIMESTAMPTZ,
    status_llm_mode TEXT NOT NULL DEFAULT 'managed',
    status_llm_base_url TEXT,
    status_llm_model TEXT,
    status_llm_api_key BYTEA,
    status_llm_key_source TEXT NOT NULL DEFAULT 'server_wrapped'
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS enrollment_state TEXT NOT NULL DEFAULT 'active';
ALTER TABLE users ADD COLUMN IF NOT EXISTS enrollment_requested_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS enrollment_approved_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'default';
ALTER TABLE users ADD COLUMN IF NOT EXISTS max_frames_per_hour INT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS max_storage_mb INT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS wrapped_data_key BYTEA;
ALTER TABLE users ADD COLUMN IF NOT EXISTS data_key_source TEXT NOT NULL DEFAULT 'server_wrapped';
ALTER TABLE users ADD COLUMN IF NOT EXISTS client_key_last_seen_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS data_key_created_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS data_key_rotated_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS status_llm_mode TEXT NOT NULL DEFAULT 'managed';
ALTER TABLE users ADD COLUMN IF NOT EXISTS status_llm_base_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS status_llm_model TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS status_llm_api_key BYTEA;
ALTER TABLE users ADD COLUMN IF NOT EXISTS status_llm_key_source TEXT NOT NULL DEFAULT 'server_wrapped';

CREATE TABLE IF NOT EXISTS devices (
    user_pubkey   TEXT NOT NULL REFERENCES users(user_pubkey) ON DELETE CASCADE,
    device_pubkey TEXT NOT NULL CHECK (length(device_pubkey) = 64),
    label         TEXT,
    created_at    TIMESTAMPTZ DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    PRIMARY KEY (user_pubkey, device_pubkey)
);

CREATE TABLE IF NOT EXISTS frames (
    id          BIGSERIAL PRIMARY KEY,
    user_pubkey TEXT,
    device_pubkey TEXT,
    ts          TIMESTAMPTZ NOT NULL,
    app         TEXT,
    bundle_id   TEXT,
    "window"    BYTEA,          -- Fernet-encrypted
    ocr_text    BYTEA,          -- Fernet-encrypted
    urls        BYTEA,          -- Fernet-encrypted JSON
    image_key   TEXT,           -- R2 path (content is encrypted, path is not)
    width       INT,
    height      INT,
    tier_hint   INT,
    routing     JSONB,
    activity    BYTEA,          -- Fernet-encrypted activity status (category + detail)
    data_key_source TEXT NOT NULL DEFAULT 'server_wrapped',
    created_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE frames ADD COLUMN IF NOT EXISTS user_pubkey TEXT;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS device_pubkey TEXT;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS data_key_source TEXT NOT NULL DEFAULT 'server_wrapped';

CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames (ts);
CREATE INDEX IF NOT EXISTS idx_frames_bundle ON frames (bundle_id);
CREATE INDEX IF NOT EXISTS idx_frames_user_ts ON frames (user_pubkey, ts DESC);
CREATE INDEX IF NOT EXISTS idx_frames_user_activity_ts
    ON frames (user_pubkey, ts DESC)
    WHERE activity IS NOT NULL;

-- Audio transcripts captured during meetings/calls. Daemon only forwards
-- these while its meeting detector says the user is in a call.
CREATE TABLE IF NOT EXISTS audio_transcripts (
    id              BIGSERIAL PRIMARY KEY,
    user_pubkey     TEXT,
    device_pubkey   TEXT,
    ts              TIMESTAMPTZ NOT NULL,
    meeting_app     TEXT,           -- e.g. "zoom", "google_meet", "wechat"
    device_name     TEXT,           -- audio device the transcript came from
    is_input_device BOOLEAN,        -- true = mic, false = system output
    transcript      BYTEA,          -- Fernet-encrypted
    data_key_source TEXT NOT NULL DEFAULT 'server_wrapped',
    created_at      TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE audio_transcripts ADD COLUMN IF NOT EXISTS user_pubkey TEXT;
ALTER TABLE audio_transcripts ADD COLUMN IF NOT EXISTS device_pubkey TEXT;
ALTER TABLE audio_transcripts ADD COLUMN IF NOT EXISTS data_key_source TEXT NOT NULL DEFAULT 'server_wrapped';

CREATE INDEX IF NOT EXISTS idx_audio_ts ON audio_transcripts (ts);
CREATE INDEX IF NOT EXISTS idx_audio_app ON audio_transcripts (meeting_app);
CREATE INDEX IF NOT EXISTS idx_audio_user_ts ON audio_transcripts (user_pubkey, ts DESC);

-- Agent/deputy access for backend-backed context reads. Deputies are
-- scoped per user tenant and authenticate with their own FishKey. Owners
-- provision and revoke these rows with their own FishKey.
CREATE TABLE IF NOT EXISTS deputies (
    user_pubkey   TEXT NOT NULL REFERENCES users(user_pubkey) ON DELETE CASCADE,
    deputy_pubkey TEXT NOT NULL CHECK (length(deputy_pubkey) = 64),
    name          TEXT,
    scopes        JSONB NOT NULL DEFAULT '[]'::jsonb,
    rate_per_hour INT,
    expires_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    added_at      TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_pubkey, deputy_pubkey)
);

CREATE INDEX IF NOT EXISTS idx_deputies_user_active
    ON deputies(user_pubkey, deputy_pubkey)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS deputy_rate_events (
    user_pubkey TEXT NOT NULL,
    deputy_pubkey TEXT NOT NULL CHECK (length(deputy_pubkey) = 64),
    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_deputy_rate_events_window
    ON deputy_rate_events(user_pubkey, deputy_pubkey, ts DESC);

-- Metadata-only audit trail for context reads and status-generation
-- reads. Never store raw OCR, transcripts, window titles, prompts, or
-- generated status text in this table.
CREATE TABLE IF NOT EXISTS access_audit_events (
    id BIGSERIAL PRIMARY KEY,
    user_pubkey TEXT NOT NULL,
    actor_pubkey TEXT,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    scope TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_access_audit_user_created
    ON access_audit_events(user_pubkey, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_access_audit_actor_created
    ON access_audit_events(actor_pubkey, created_at DESC);
