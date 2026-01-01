-- Optional performance migration: speed up keyword-based search on listings.metadata
-- Run this in Supabase SQL editor (or as a migration) when you enable keyword metadata search.

-- Trigram extension is commonly used already (idx_listings_title_trgm). Keep it idempotent.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Index the derived keywords_text blob for fast ILIKE queries.
-- We coalesce to '' so the expression is always text.
CREATE INDEX IF NOT EXISTS idx_listings_metadata_keywords_text_trgm
ON public.listings
USING gin ((coalesce(metadata->>'keywords_text','')) gin_trgm_ops);
