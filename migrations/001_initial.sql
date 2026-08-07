-- Initial schema.
--
-- SQLite is the authoritative record of *state*; the filesystem is the
-- authoritative record of *evidence*. Where the two disagree, startup
-- reconciliation trusts the artifact and repairs the row — an artifact that
-- exists was fsynced and renamed atomically, whereas a row can be written by a
-- transaction that is later rolled back.
--
-- Every table carries created_at/updated_at as ISO-8601 UTC text. SQLite has no
-- native timestamp type and storing text keeps the database readable with any
-- sqlite3 shell, which matters for recovery.

-- ── Jobs ──────────────────────────────────────────────────────────────────

CREATE TABLE jobs (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL,
    output_root         TEXT NOT NULL,
    frame_interval_ms   INTEGER,
    visual_provider     TEXT NOT NULL DEFAULT 'none',
    visual_model_id     TEXT NOT NULL DEFAULT '',
    budget_limit_usd    REAL,
    budget_spent_usd    REAL NOT NULL DEFAULT 0.0,
    budget_on_limit     TEXT NOT NULL DEFAULT 'stop_and_ask',
    settings_json       TEXT NOT NULL DEFAULT '{}',
    error_message       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,

    CHECK (status IN (
        'draft', 'ready', 'preparing', 'transcribing', 'analyzing',
        'waiting_retry', 'paused', 'needs_attention',
        'completed', 'completed_with_gaps', 'cancelled'
    )),
    -- Once extraction begins the interval is immutable. A different interval
    -- means a deliberate new version, never a mutation of this one.
    CHECK (frame_interval_ms IS NULL OR frame_interval_ms BETWEEN 500 AND 10000)
);

CREATE INDEX idx_jobs_status ON jobs (status);
CREATE INDEX idx_jobs_updated ON jobs (updated_at DESC);

-- ── Videos within a job ───────────────────────────────────────────────────

CREATE TABLE job_videos (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    -- Sources are referenced by absolute path and never copied or moved.
    source_path         TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    -- SHA-256 of the source file, computed before the file is accepted, so a
    -- duplicate is caught before any expensive work begins.
    source_sha256       TEXT,
    duration_seconds    REAL,
    container           TEXT,
    width               INTEGER,
    height              INTEGER,
    -- Explicit user-confirmed ordering. Never inferred from filename or date.
    sequence            INTEGER NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    is_active_version   INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'pending',
    frame_count         INTEGER,
    output_dir          TEXT,
    imported_from       TEXT,
    error_message       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,

    CHECK (sequence >= 0),
    CHECK (version >= 1),
    CHECK (is_active_version IN (0, 1)),
    CHECK (status IN (
        'pending', 'preparing', 'transcribing', 'analyzing', 'waiting_retry',
        'paused', 'needs_attention', 'completed', 'completed_with_gaps',
        'cancelled', 'skipped'
    )),
    UNIQUE (job_id, sequence, version)
);

CREATE INDEX idx_job_videos_job ON job_videos (job_id, sequence);
CREATE INDEX idx_job_videos_sha ON job_videos (source_sha256);
CREATE INDEX idx_job_videos_active ON job_videos (is_active_version, status);

-- ── Stage runs ────────────────────────────────────────────────────────────
--
-- One row per (video, stage, attempt). Attempts are kept rather than
-- overwritten: a rerun must never destroy the provenance of what came before,
-- particularly when the earlier run cost money.

CREATE TABLE stage_runs (
    id                  TEXT PRIMARY KEY,
    job_video_id        TEXT NOT NULL REFERENCES job_videos (id) ON DELETE CASCADE,
    stage               TEXT NOT NULL,
    attempt             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL,
    backend             TEXT,
    fell_back_from      TEXT,
    provider            TEXT,
    model_id            TEXT,
    prompt_hash         TEXT,
    schema_hash         TEXT,
    output_version      INTEGER,
    items_total         INTEGER,
    items_done          INTEGER NOT NULL DEFAULT 0,
    items_skipped       INTEGER NOT NULL DEFAULT 0,
    provenance_json     TEXT NOT NULL DEFAULT '{}',
    error_message       TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,

    CHECK (stage IN ('frames', 'transcribe', 'visual', 'enrich', 'assemble', 'package')),
    CHECK (status IN (
        'pending', 'running', 'waiting_retry', 'paused',
        'completed', 'completed_with_gaps', 'failed', 'cancelled'
    )),
    CHECK (attempt >= 1),
    UNIQUE (job_video_id, stage, attempt)
);

CREATE INDEX idx_stage_runs_video ON stage_runs (job_video_id, stage);
CREATE INDEX idx_stage_runs_status ON stage_runs (status);

-- ── Batches ───────────────────────────────────────────────────────────────
--
-- A unit of provider work. Cloud batches hold up to 20 frames; local Ollama
-- defaults to 1. A batch is marked completed only after its artifact is
-- durably persisted, so a completed batch is never re-sent and never re-billed.

CREATE TABLE batches (
    id                  TEXT PRIMARY KEY,
    stage_run_id        TEXT NOT NULL REFERENCES stage_runs (id) ON DELETE CASCADE,
    batch_index         INTEGER NOT NULL,
    frame_start_index   INTEGER NOT NULL,
    frame_end_index     INTEGER NOT NULL,
    frame_count         INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    attempt             INTEGER NOT NULL DEFAULT 0,
    provider            TEXT,
    model_id            TEXT,
    -- Local runs record no cost at all rather than a zero, so the interface can
    -- say "No provider API charge" instead of the misleading "$0.00".
    cost_usd            REAL,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    latency_ms          INTEGER,
    retry_history_json  TEXT NOT NULL DEFAULT '[]',
    skip_reason         TEXT,
    artifact_path       TEXT,
    artifact_sha256     TEXT,
    error_message       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,

    CHECK (status IN (
        'pending', 'running', 'waiting_retry', 'completed', 'failed', 'skipped', 'cancelled'
    )),
    CHECK (frame_count > 0),
    CHECK (frame_end_index >= frame_start_index),
    UNIQUE (stage_run_id, batch_index)
);

CREATE INDEX idx_batches_run ON batches (stage_run_id, batch_index);
CREATE INDEX idx_batches_status ON batches (status);

-- ── Artifacts ─────────────────────────────────────────────────────────────
--
-- The registry of everything written to disk, with a checksum. Startup
-- reconciliation walks this table to detect files that vanished, and the
-- filesystem to detect files no row knows about.

CREATE TABLE artifacts (
    id                  TEXT PRIMARY KEY,
    job_id              TEXT REFERENCES jobs (id) ON DELETE CASCADE,
    job_video_id        TEXT REFERENCES job_videos (id) ON DELETE CASCADE,
    collection_build_id TEXT,
    kind                TEXT NOT NULL,
    -- Relative to the output root, so moving the output root does not
    -- invalidate every row and no absolute path is recorded.
    relative_path       TEXT NOT NULL,
    size_bytes          INTEGER,
    sha256              TEXT,
    created_at          TEXT NOT NULL,

    CHECK (kind IN (
        'frames_dir', 'frames_api_dir', 'frames_manifest', 'audio',
        'transcript_raw', 'transcript', 'silence_windows', 'visual_batch',
        'visual_results', 'gaps', 'assembled', 'master_assembled',
        'provenance', 'analysis_input', 'readme',
        'collection_assembled', 'collection_manifest', 'collection_readme',
        'collection_pack', 'collection_pack_manifest'
    ))
);

CREATE UNIQUE INDEX idx_artifacts_path ON artifacts (relative_path);
CREATE INDEX idx_artifacts_job ON artifacts (job_id);
CREATE INDEX idx_artifacts_video ON artifacts (job_video_id);
CREATE INDEX idx_artifacts_build ON artifacts (collection_build_id);

-- ── Events ────────────────────────────────────────────────────────────────
--
-- The human-readable recovery log shown on the job screen. Append-only.

CREATE TABLE events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              TEXT REFERENCES jobs (id) ON DELETE CASCADE,
    job_video_id        TEXT REFERENCES job_videos (id) ON DELETE CASCADE,
    collection_id       TEXT,
    level               TEXT NOT NULL DEFAULT 'info',
    kind                TEXT NOT NULL,
    -- Plain language, written for the person reading the screen. Passed through
    -- redaction before it is stored.
    message             TEXT NOT NULL,
    detail_json         TEXT,
    created_at          TEXT NOT NULL,

    CHECK (level IN ('info', 'warning', 'error'))
);

CREATE INDEX idx_events_job ON events (job_id, created_at DESC);
CREATE INDEX idx_events_created ON events (created_at DESC);

-- ── Worker claim ──────────────────────────────────────────────────────────
--
-- One worker per output root. The filesystem lock is the primary guard; this
-- table is the second, so a stale lock file on a crashed machine cannot produce
-- two workers writing the same artifacts.

CREATE TABLE worker_claims (
    output_root         TEXT PRIMARY KEY,
    worker_id           TEXT NOT NULL,
    hostname            TEXT NOT NULL,
    pid                 INTEGER NOT NULL,
    heartbeat_at        TEXT NOT NULL,
    claimed_at          TEXT NOT NULL
);

-- ── Collections ───────────────────────────────────────────────────────────

CREATE TABLE collections (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    mode                TEXT NOT NULL DEFAULT 'full',
    token_limit         INTEGER,
    reserve_tokens      INTEGER,
    target_model_label  TEXT NOT NULL DEFAULT '',
    allow_video_split   INTEGER NOT NULL DEFAULT 0,
    current_version     INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,

    CHECK (mode IN ('full', 'packs')),
    CHECK (allow_video_split IN (0, 1))
);

CREATE INDEX idx_collections_updated ON collections (updated_at DESC);

-- Immutable references to the exact processed-video versions a collection uses.
-- A source video being reprocessed later must not change an existing
-- collection: the user rebuilds deliberately, or creates a new one.
CREATE TABLE collection_sources (
    id                  TEXT PRIMARY KEY,
    collection_id       TEXT NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
    job_video_id        TEXT NOT NULL REFERENCES job_videos (id),
    source_version      INTEGER NOT NULL,
    sequence            INTEGER NOT NULL,
    display_name        TEXT NOT NULL,
    duration_seconds    REAL,
    assembled_sha256    TEXT,
    warning_state       TEXT NOT NULL DEFAULT 'ok',
    warning_detail      TEXT,
    created_at          TEXT NOT NULL,

    CHECK (sequence >= 0),
    CHECK (source_version >= 1),
    CHECK (warning_state IN ('ok', 'gaps', 'no_visual', 'provenance_mismatch', 'missing_artifacts')),
    UNIQUE (collection_id, sequence)
);

CREATE INDEX idx_collection_sources_collection ON collection_sources (collection_id, sequence);

CREATE TABLE collection_builds (
    id                  TEXT PRIMARY KEY,
    collection_id       TEXT NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
    collection_version  INTEGER NOT NULL,
    mode                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    output_dir          TEXT,
    pack_count          INTEGER,
    total_tokens_est    INTEGER,
    token_method        TEXT NOT NULL DEFAULT '',
    packing_algorithm   TEXT NOT NULL DEFAULT '',
    packing_version     INTEGER,
    warning_count       INTEGER NOT NULL DEFAULT 0,
    manifest_sha256     TEXT,
    error_message       TEXT,
    created_at          TEXT NOT NULL,
    completed_at        TEXT,

    CHECK (mode IN ('full', 'packs')),
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    UNIQUE (collection_id, collection_version)
);

CREATE INDEX idx_collection_builds_collection ON collection_builds (collection_id, collection_version DESC);
