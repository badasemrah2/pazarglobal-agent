-- Migration: Fix active_drafts uniqueness constraint
-- Purpose: Prevent duplicate drafts per user (Race condition fix)
-- Applied: [DATE] by [USER]
-- Rollback: DROP CONSTRAINT active_drafts_user_id_unique; DROP INDEX idx_active_drafts_user_id;

-- 1. Check for existing duplicate drafts (audit before migration)
DO $$ 
DECLARE 
    duplicate_count INT;
BEGIN
    SELECT COUNT(*) INTO duplicate_count
    FROM (
        SELECT user_id, COUNT(*) as cnt
        FROM public.active_drafts
        WHERE user_id IS NOT NULL
        GROUP BY user_id
        HAVING COUNT(*) > 1
    ) dupes;
    
    IF duplicate_count > 0 THEN
        RAISE NOTICE 'WARNING: % users have duplicate drafts. Keeping newest draft only.', duplicate_count;
        
        -- Keep only the most recent draft per user
        DELETE FROM public.active_drafts
        WHERE id NOT IN (
            SELECT DISTINCT ON (user_id) id
            FROM public.active_drafts
            WHERE user_id IS NOT NULL
            ORDER BY user_id, updated_at DESC
        );
        
        RAISE NOTICE 'Cleaned up duplicate drafts.';
    ELSE
        RAISE NOTICE 'No duplicate drafts found. Safe to proceed.';
    END IF;
END $$;

-- 2. Add UNIQUE constraint to enforce single draft per user
ALTER TABLE public.active_drafts 
ADD CONSTRAINT active_drafts_user_id_unique UNIQUE (user_id);

-- 3. Add index for faster lookups (used by get_latest_draft_for_user)
CREATE INDEX IF NOT EXISTS idx_active_drafts_user_id 
ON public.active_drafts(user_id)
WHERE user_id IS NOT NULL;

-- 4. Add foreign key constraint for cascade delete
ALTER TABLE public.active_drafts
ADD CONSTRAINT fk_active_drafts_user 
FOREIGN KEY (user_id) REFERENCES public.profiles(id) 
ON DELETE CASCADE;

-- 5. Add updated_at trigger for timestamp management
CREATE OR REPLACE FUNCTION update_active_drafts_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_active_drafts_updated_at ON public.active_drafts;
CREATE TRIGGER trigger_active_drafts_updated_at
    BEFORE UPDATE ON public.active_drafts
    FOR EACH ROW
    EXECUTE FUNCTION update_active_drafts_updated_at();

-- Migration complete
COMMENT ON CONSTRAINT active_drafts_user_id_unique ON public.active_drafts 
IS 'Enforces single draft per user. Added January 22, 2026 to prevent race conditions.';

-- Verification: Run these manually AFTER migration if you want to verify
-- SELECT constraint_name FROM information_schema.table_constraints 
-- WHERE table_name = 'active_drafts' AND constraint_name = 'active_drafts_user_id_unique';
-- Expected: 1 row returned with constraint_name

-- Test constraint behavior in test_concurrency.py (not in migration)
