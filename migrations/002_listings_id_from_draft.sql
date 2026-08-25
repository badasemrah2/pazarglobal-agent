-- Ensure listings.id accepts externally provided draft_id values
-- Safe for UUID PKs; drops identity/default if present.

ALTER TABLE public.listings
  ALTER COLUMN id DROP DEFAULT;

ALTER TABLE public.listings
  ALTER COLUMN id DROP IDENTITY IF EXISTS;

-- Optional: enforce UUID type if your schema already uses UUIDs
-- (Uncomment only if id is already UUID-compatible)
-- ALTER TABLE public.listings
--   ALTER COLUMN id TYPE uuid USING id::uuid;
