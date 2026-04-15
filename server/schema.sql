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
    scene       BYTEA,          -- Fernet-encrypted VLM description
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
