CREATE TABLE IF NOT EXISTS users (
    user_pubkey TEXT PRIMARY KEY CHECK (length(user_pubkey) = 64),
    created_at  TIMESTAMPTZ DEFAULT now(),
    disabled_at TIMESTAMPTZ
);

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
    created_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE frames ADD COLUMN IF NOT EXISTS user_pubkey TEXT;
ALTER TABLE frames ADD COLUMN IF NOT EXISTS device_pubkey TEXT;

CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames (ts);
CREATE INDEX IF NOT EXISTS idx_frames_bundle ON frames (bundle_id);
CREATE INDEX IF NOT EXISTS idx_frames_user_ts ON frames (user_pubkey, ts DESC);
CREATE INDEX IF NOT EXISTS idx_frames_user_activity_ts
    ON frames (user_pubkey, ts DESC)
    WHERE activity IS NOT NULL;

-- Pokes: lightweight nudges between friends
CREATE TABLE IF NOT EXISTS pokes (
    id          BIGSERIAL PRIMARY KEY,
    to_pubkey   TEXT,
    from_pubkey TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE pokes ADD COLUMN IF NOT EXISTS to_pubkey TEXT;

CREATE INDEX IF NOT EXISTS idx_pokes_created ON pokes (created_at);
CREATE INDEX IF NOT EXISTS idx_pokes_to_created ON pokes (to_pubkey, created_at DESC);

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
    created_at      TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE audio_transcripts ADD COLUMN IF NOT EXISTS user_pubkey TEXT;
ALTER TABLE audio_transcripts ADD COLUMN IF NOT EXISTS device_pubkey TEXT;

CREATE INDEX IF NOT EXISTS idx_audio_ts ON audio_transcripts (ts);
CREATE INDEX IF NOT EXISTS idx_audio_app ON audio_transcripts (meeting_app);
CREATE INDEX IF NOT EXISTS idx_audio_user_ts ON audio_transcripts (user_pubkey, ts DESC);
