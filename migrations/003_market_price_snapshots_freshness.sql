-- 003_market_price_snapshots_freshness.sql
-- Purpose:
-- 1) Improve performance for fresh snapshot reads.
-- 2) Normalize missing last_updated_at values.
-- 3) Provide DB-side cleanup helper for max 30-day retention.

BEGIN;

-- Backfill null last_updated_at from created_at (or now as final fallback)
UPDATE public.market_price_snapshots
SET last_updated_at = COALESCE(last_updated_at, created_at, now())
WHERE last_updated_at IS NULL;

-- Index used by freshness filter (>= now() - interval '31 days')
CREATE INDEX IF NOT EXISTS idx_market_price_snapshots_last_updated_at
ON public.market_price_snapshots (last_updated_at DESC);

-- Composite index helps filtered lookups by product/category/condition + freshness
CREATE INDEX IF NOT EXISTS idx_market_price_snapshots_lookup_fresh
ON public.market_price_snapshots (product_key, category, condition, last_updated_at DESC);

-- Optional hardening: keep future inserts from leaving timestamp null
ALTER TABLE public.market_price_snapshots
ALTER COLUMN last_updated_at SET DEFAULT now();

-- Cleanup helper function: keep only recent rows (default 30 days)
CREATE OR REPLACE FUNCTION public.cleanup_old_market_price_snapshots(retention_days integer DEFAULT 30)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    deleted_count bigint;
BEGIN
    DELETE FROM public.market_price_snapshots
    WHERE COALESCE(last_updated_at, created_at, now()) < now() - make_interval(days => retention_days);

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$;

COMMIT;

-- One-time immediate cleanup after migration (safe to run repeatedly)
SELECT public.cleanup_old_market_price_snapshots(30);

-- Optional: schedule daily cleanup if pg_cron is available.
-- SELECT cron.schedule(
--   'cleanup-market-price-snapshots-daily',
--   '15 3 * * *',
--   $$SELECT public.cleanup_old_market_price_snapshots(30);$$
-- );
