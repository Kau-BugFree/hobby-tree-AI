-- Apply with psql -v ON_ERROR_STOP=1 -f sql/001_catalog.sql.
-- This migration owns only item_recommender. Times stored in columns are UTC Unix seconds.
CREATE SCHEMA IF NOT EXISTS item_recommender;

CREATE TABLE IF NOT EXISTS item_recommender.catalog_queries (
    query_key text PRIMARY KEY,
    query_text text NOT NULL,
    active integer NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    origin text NOT NULL DEFAULT 'MANUAL',
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS item_recommender.material_source_refs (
    source_id TEXT NOT NULL,
    source_epoch TEXT NOT NULL,
    material_id BIGINT NOT NULL CHECK (material_id > 0),
    activity_id BIGINT NOT NULL CHECK (activity_id > 0),
    hobby_id BIGINT NOT NULL CHECK (hobby_id > 0),
    activity_content_version BIGINT NOT NULL CHECK (activity_content_version > 0),
    raw_name TEXT NOT NULL CHECK (length(raw_name) BETWEEN 1 AND 160),
    query_key TEXT NOT NULL REFERENCES item_recommender.catalog_queries(query_key),
    PRIMARY KEY (source_id, source_epoch, material_id)
);
ALTER TABLE item_recommender.material_source_refs
    ADD COLUMN IF NOT EXISTS is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1));
CREATE TABLE IF NOT EXISTS item_recommender.material_export_receipts (
    source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
    source_sequence BIGINT NOT NULL CHECK (source_sequence > 0),
    payload_sha256 TEXT NOT NULL, accepted_at DOUBLE PRECISION NOT NULL,
    approval_digest TEXT,
    PRIMARY KEY (source_id, source_sequence)
);
CREATE INDEX IF NOT EXISTS material_source_refs_by_key
    ON item_recommender.material_source_refs(query_key, source_id, source_epoch);
CREATE TABLE IF NOT EXISTS item_recommender.query_fallbacks (
    primary_key text NOT NULL REFERENCES item_recommender.catalog_queries(query_key),
    fallback_key text NOT NULL REFERENCES item_recommender.catalog_queries(query_key),
    position integer NOT NULL CHECK (position >= 0),
    PRIMARY KEY (primary_key, fallback_key),
    UNIQUE (primary_key, position),
    CHECK (primary_key <> fallback_key)
);
CREATE TABLE IF NOT EXISTS item_recommender.catalog_snapshots (
    query_key text NOT NULL REFERENCES item_recommender.catalog_queries(query_key),
    source text NOT NULL CHECK (source IN ('BATCH', 'CURATED')),
    offers_json text CHECK (offers_json IS NULL OR octet_length(offers_json) <= 262144),
    revision text,
    fetched_at double precision,
    expires_at double precision,
    last_attempt_at double precision,
    last_status text NOT NULL DEFAULT 'NOT_COLLECTED',
    discarded_count integer NOT NULL DEFAULT 0 CHECK (discarded_count >= 0),
    discard_reasons_json text NOT NULL DEFAULT '[]',
    PRIMARY KEY (query_key, source)
);
CREATE TABLE IF NOT EXISTS item_recommender.refresh_jobs (
    job_id text PRIMARY KEY,
    query_key text NOT NULL REFERENCES item_recommender.catalog_queries(query_key),
    run_date text NOT NULL,
    status text NOT NULL DEFAULT 'QUEUED'
        CHECK (status IN ('QUEUED', 'LEASED', 'DONE', 'FAILED', 'EXPIRED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    lease_token text,
    lease_until double precision,
    next_attempt_at double precision NOT NULL,
    created_at double precision NOT NULL,
    updated_at double precision NOT NULL,
    last_error text,
    UNIQUE (query_key, run_date)
);
CREATE INDEX IF NOT EXISTS refresh_jobs_claim
    ON item_recommender.refresh_jobs(run_date, status, next_attempt_at);
CREATE INDEX IF NOT EXISTS catalog_snapshots_expiry
    ON item_recommender.catalog_snapshots(expires_at) WHERE offers_json IS NOT NULL;

-- Only the SHA-256 namespace and operating timestamps are stored in quota tables.
CREATE TABLE IF NOT EXISTS item_recommender.provider_locks (
    namespace text PRIMARY KEY CHECK (namespace ~ '^[0-9a-f]{64}$')
);
CREATE TABLE IF NOT EXISTS item_recommender.provider_calls (
    namespace text NOT NULL CHECK (namespace ~ '^[0-9a-f]{64}$'),
    called_at double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS catalog_provider_window
    ON item_recommender.provider_calls(namespace, called_at);

CREATE OR REPLACE FUNCTION item_recommender.purge_expired_catalog(
    p_now timestamptz DEFAULT clock_timestamp()
) RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog, item_recommender
AS $$
DECLARE
    changed integer;
BEGIN
    UPDATE item_recommender.catalog_snapshots
       SET offers_json = NULL
     WHERE offers_json IS NOT NULL AND expires_at <= extract(epoch FROM p_now);
    GET DIAGNOSTICS changed = ROW_COUNT;
    DELETE FROM item_recommender.provider_calls
     WHERE called_at <= extract(epoch FROM p_now) - 60;
    RETURN changed;
END;
$$;

CREATE OR REPLACE FUNCTION item_recommender.enqueue_daily_refresh(
    p_now timestamptz DEFAULT clock_timestamp()
) RETURNS integer
LANGUAGE plpgsql
SET search_path = pg_catalog, item_recommender
AS $$
DECLARE
    seoul_now timestamp := p_now AT TIME ZONE 'Asia/Seoul';
    seoul_date text := to_char(seoul_now, 'YYYY-MM-DD');
    now_epoch double precision := extract(epoch FROM p_now);
    inserted integer;
BEGIN
    -- Retention cleanup must also run when invoked outside the collection window.
    PERFORM item_recommender.purge_expired_catalog(p_now);
    IF extract(hour FROM seoul_now) < 3 OR extract(hour FROM seoul_now) >= 5 THEN
        RETURN 0;
    END IF;
    UPDATE item_recommender.refresh_jobs
       SET status = 'EXPIRED', lease_token = NULL, lease_until = NULL, updated_at = now_epoch
     WHERE run_date < seoul_date AND status IN ('QUEUED', 'LEASED');
    INSERT INTO item_recommender.refresh_jobs
        (job_id, query_key, run_date, next_attempt_at, created_at, updated_at)
    SELECT md5(q.query_key || '|' || seoul_date), q.query_key, seoul_date,
           now_epoch, now_epoch, now_epoch
      FROM item_recommender.catalog_queries q
     WHERE q.active = 1
     ORDER BY q.query_key
    -- The deterministic job_id and (query_key, run_date) are both unique.
    -- Concurrent schedulers may collide on either index, so handle both.
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS inserted = ROW_COUNT;
    RETURN inserted;
END;
$$;
