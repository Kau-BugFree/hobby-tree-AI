-- Optional administrator migration; run after 001_catalog.sql in the cron-enabled database.
-- PostgreSQL must already have pg_cron installed and shared_preload_libraries='pg_cron'.
-- No HTTP request is made from PostgreSQL. These jobs only enqueue work and purge payloads.
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Named schedule calls replace the same owner's existing jobs instead of duplicating them.
-- Every minute, independently of cron.timezone or the server's session timezone.
SELECT cron.schedule(
    'hobbytree-item-catalog-enqueue', '* * * * *',
    'SELECT item_recommender.enqueue_daily_refresh();'
);
SELECT cron.schedule(
    'hobbytree-item-catalog-purge', '* * * * *',
    'SELECT item_recommender.purge_expired_catalog();'
);

-- Validate: SELECT jobname, schedule, command, active FROM cron.job
-- WHERE jobname IN ('hobbytree-item-catalog-enqueue', 'hobbytree-item-catalog-purge');
-- The enqueue function itself enforces 03:00 <= Asia/Seoul time < 05:00.
-- Expired payloads stop being served at expires_at; physical deletion occurs on the next run.
