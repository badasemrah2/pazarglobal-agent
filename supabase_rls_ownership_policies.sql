-- Row Level Security Policies for Ownership Verification
-- ⚠️ DISABLED FOR SERVICE ROLE KEY COMPATIBILITY ⚠️
--
-- RLS policies would block service role key (backend server) operations.
-- Service role key is used for admin operations and needs unrestricted access.
--
-- SECURITY APPROACH:
-- - Application-level checks in supabase_client.py (ACTIVE)
-- - FSM-level checks in webchat.py (ACTIVE)  
-- - Database RLS policies (DISABLED - incompatible with service role)
--
-- To enable RLS, you would need to:
-- 1. Use anon key instead of service role key
-- 2. Implement proper auth.uid() from Supabase Auth
-- 3. Migrate from phone-based sessions to Supabase Auth users
--
-- For now, we rely on application-level ownership verification.
-- ============================================================================

-- DISABLED: RLS policies commented out to prevent service role key blocking

/*
-- ============================================================================
-- ACTIVE_DRAFTS: Users can only access their own drafts
-- ============================================================================

-- Enable RLS on active_drafts table
ALTER TABLE active_drafts ENABLE ROW LEVEL SECURITY;

-- Users can read only their own drafts
CREATE POLICY "Users can read own drafts"
ON active_drafts FOR SELECT
USING (user_id::text = current_setting('app.user_id', true));

-- Users can insert only with their own user_id
CREATE POLICY "Users can create own drafts"
ON active_drafts FOR INSERT
WITH CHECK (user_id::text = current_setting('app.user_id', true));

-- Users can update only their own drafts
CREATE POLICY "Users can update own drafts"
ON active_drafts FOR UPDATE
USING (user_id::text = current_setting('app.user_id', true));

-- Users can delete only their own drafts
CREATE POLICY "Users can delete own drafts"
ON active_drafts FOR DELETE
USING (user_id::text = current_setting('app.user_id', true));

-- ============================================================================
-- LISTINGS: Users can only modify/delete their own listings
-- ============================================================================

-- Enable RLS on listings table
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;

-- Everyone can read active listings (public marketplace)
CREATE POLICY "Anyone can read active listings"
ON listings FOR SELECT
USING (status = 'active');

-- Users can insert only with their own user_id
CREATE POLICY "Users can create own listings"
ON listings FOR INSERT
WITH CHECK (user_id::text = current_setting('app.user_id', true));

-- Users can update only their own listings
CREATE POLICY "Users can update own listings"
ON listings FOR UPDATE
USING (user_id::text = current_setting('app.user_id', true));

-- Users can delete only their own listings
CREATE POLICY "Users can delete own listings"
ON listings FOR DELETE
USING (user_id::text = current_setting('app.user_id', true));

-- ============================================================================
-- HOW TO USE:
-- ============================================================================
-- In your Supabase client initialization, set the user context:
--
-- Python example:
--   supabase.rpc('set_user_context', {'user_id': current_user_id})
--
-- Or set before each operation:
--   await supabase.rpc('exec', {
--       'sql': "SELECT set_config('app.user_id', $1, false)",
--       'params': [user_id]
--   })
--
-- This ensures all database operations are scoped to the authenticated user.
-- ============================================================================

-- Helper function to set user context (optional, for convenience)
CREATE OR REPLACE FUNCTION set_user_context(p_user_id TEXT)
RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM set_config('app.user_id', p_user_id, false);
END;
$$;

-- ============================================================================
-- TESTING:
-- ============================================================================
-- Test ownership enforcement:
--
-- 1. Set user context:
--    SELECT set_user_context('user123');
--
-- 2. Try to update another user's draft (should fail):
--    UPDATE active_drafts 
--    SET listing_data = '{"title": "hacked"}' 
--    WHERE user_id = 'user456';
--    -- Expected: 0 rows updated (policy blocks it)
--
-- 3. Update own draft (should succeed):
--    UPDATE active_drafts 
--    SET listing_data = '{"title": "my item"}' 
--    WHERE user_id = 'user123';
--    -- Expected: 1 row updated
-- ============================================================================
*/

-- ============================================================================
-- ROLLBACK SCRIPT (if RLS was enabled):
-- ============================================================================
-- Run this in Supabase SQL Editor to disable RLS and restore service:
--
-- ALTER TABLE active_drafts DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE listings DISABLE ROW LEVEL SECURITY;
-- DROP POLICY IF EXISTS "Users can read own drafts" ON active_drafts;
-- DROP POLICY IF EXISTS "Users can create own drafts" ON active_drafts;
-- DROP POLICY IF EXISTS "Users can update own drafts" ON active_drafts;
-- DROP POLICY IF EXISTS "Users can delete own drafts" ON active_drafts;
-- DROP POLICY IF EXISTS "Anyone can read active listings" ON listings;
-- DROP POLICY IF EXISTS "Users can create own listings" ON listings;
-- DROP POLICY IF EXISTS "Users can update own listings" ON listings;
-- DROP POLICY IF EXISTS "Users can delete own listings" ON listings;
-- ============================================================================

