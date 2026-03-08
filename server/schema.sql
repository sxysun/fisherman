CREATE TABLE IF NOT EXISTS frames (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    app         TEXT,
    bundle_id   TEXT,
    window      TEXT,
    ocr_text    BYTEA,          -- Fernet-encrypted
    urls        BYTEA,          -- Fernet-encrypted JSON
    image_key   TEXT,           -- R2 path (content is encrypted, path is not)
    width       INT,
    height      INT,
    tier_hint   INT,
    routing     JSONB,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames (ts);
CREATE INDEX IF NOT EXISTS idx_frames_bundle ON frames (bundle_id);
