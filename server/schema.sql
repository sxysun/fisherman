CREATE TABLE IF NOT EXISTS frames (
    id          BIGSERIAL PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames (ts);
CREATE INDEX IF NOT EXISTS idx_frames_bundle ON frames (bundle_id);

-- Pokes: lightweight nudges between friends
CREATE TABLE IF NOT EXISTS pokes (
    id          BIGSERIAL PRIMARY KEY,
    from_pubkey TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pokes_created ON pokes (created_at);

-- Audio transcripts captured during meetings/calls. Daemon only forwards
-- these while its meeting detector says the user is in a call.
CREATE TABLE IF NOT EXISTS audio_transcripts (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    meeting_app     TEXT,           -- e.g. "zoom", "google_meet", "wechat"
    device_name     TEXT,           -- audio device the transcript came from
    is_input_device BOOLEAN,        -- true = mic, false = system output
    transcript      BYTEA,          -- Fernet-encrypted
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audio_ts ON audio_transcripts (ts);
CREATE INDEX IF NOT EXISTS idx_audio_app ON audio_transcripts (meeting_app);
