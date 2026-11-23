-- OPTIONAL: execute only where the backend's public tables exist and SELECT is authorized.
-- Reads activity/material tables; every mutation targets the owned item_recommender schema.
-- PostgreSQL must use UTF8. normalize(..., NFKC) requires PostgreSQL 13 or later.
BEGIN;
WITH material_queries AS (
    SELECT DISTINCT lower(regexp_replace(btrim(normalize(m.material_name, NFKC)), '\s+', ' ', 'g')) AS name
      FROM public.activity_materials m
      JOIN public.activities a ON a.activity_id = m.activity_id
      JOIN public.hobby_stages hs ON hs.hobby_stage_id = a.hobby_stage_id
      JOIN public.hobbies h ON h.hobby_id = hs.hobby_id
     WHERE a.review_status = 'PUBLISHED'
       AND h.is_supported = true
       AND m.material_name IS NOT NULL
), valid_queries AS (
    SELECT name FROM material_queries WHERE length(name) BETWEEN 1 AND 200
)
INSERT INTO item_recommender.catalog_queries
    (query_key, query_text, active, origin, created_at, updated_at)
SELECT name, name, 1, 'BE_MATERIAL', extract(epoch FROM clock_timestamp()),
       extract(epoch FROM clock_timestamp()) FROM valid_queries
ON CONFLICT (query_key) DO UPDATE
    SET active = 1, updated_at = excluded.updated_at;

-- Disable only automatically imported names that no published activity still references.
-- Explicitly registered/curated queries keep their MANUAL origin and remain active.
UPDATE item_recommender.catalog_queries q
   SET active = 0, updated_at = extract(epoch FROM clock_timestamp())
 WHERE q.origin = 'BE_MATERIAL'
   AND NOT EXISTS (
       SELECT 1 FROM public.activity_materials m
       JOIN public.activities a ON a.activity_id = m.activity_id
        WHERE a.review_status = 'PUBLISHED'
          AND m.material_name IS NOT NULL
          AND lower(regexp_replace(btrim(normalize(m.material_name, NFKC)), '\s+', ' ', 'g')) = q.query_key
   );
COMMIT;

-- For non-Korean Unicode case folding (e.g. ß), prefer application register_queries(),
-- whose Python NFKC+casefold normalization is authoritative. PostgreSQL lower() is not casefold().
-- This is a one-shot synchronization; schedule it explicitly if the backend owner approves.
