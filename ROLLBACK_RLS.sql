-- ============================================================================
-- EMERGENCY ROLLBACK: Disable RLS and restore service
-- ============================================================================
-- Run this immediately in Supabase SQL Editor if Railway crashed after RLS deployment
-- This will restore service role key access to all tables

-- Disable RLS on both tables
ALTER TABLE active_drafts DISABLE ROW LEVEL SECURITY;
ALTER TABLE listings DISABLE ROW LEVEL SECURITY;

-- Drop all policies (if they exist)
DROP POLICY IF EXISTS "Users can read own drafts" ON active_drafts;
DROP POLICY IF EXISTS "Users can create own drafts" ON active_drafts;
DROP POLICY IF EXISTS "Users can update own drafts" ON active_drafts;
DROP POLICY IF EXISTS "Users can delete own drafts" ON active_drafts;
DROP POLICY IF EXISTS "Anyone can read active listings" ON listings;
DROP POLICY IF EXISTS "Users can create own listings" ON listings;
DROP POLICY IF EXISTS "Users can update own listings" ON listings;
DROP POLICY IF EXISTS "Users can delete own listings" ON listings;

-- Verify RLS is disabled
SELECT 
    schemaname, 
    tablename, 
    rowsecurity as rls_enabled
FROM pg_tables 
WHERE tablename IN ('active_drafts', 'listings');
-- Expected: rls_enabled = false for both tables

-- SUCCESS: Service should be restored after running this script
