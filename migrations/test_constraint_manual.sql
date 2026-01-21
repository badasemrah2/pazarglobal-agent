-- Manual Constraint Test (OPTIONAL - Run AFTER migration)
-- This verifies the UNIQUE constraint works without modifying production data
-- Safe to run: Uses existing user, attempts duplicate draft, then rolls back

-- YOUR USER ID
DO $$
DECLARE
    test_user_id uuid := '3ec55e9d-93e8-40c5-8e0e-7dc933da997f'::uuid;
    existing_draft_id uuid;
    test_passed boolean := false;
BEGIN
    RAISE NOTICE 'Testing UNIQUE constraint with user %', test_user_id;
    
    -- Check if user already has a draft
    SELECT id INTO existing_draft_id 
    FROM public.active_drafts 
    WHERE user_id = test_user_id 
    LIMIT 1;
    
    IF existing_draft_id IS NOT NULL THEN
        RAISE NOTICE 'User already has draft %. Testing duplicate prevention...', existing_draft_id;
        
        -- Try to create duplicate (should fail)
        BEGIN
            INSERT INTO public.active_drafts (user_id, state, listing_data)
            VALUES (test_user_id, 'draft', '{"test": true}'::jsonb);
            
            RAISE EXCEPTION 'TEST FAILED: Duplicate draft was allowed!';
        EXCEPTION
            WHEN unique_violation THEN
                test_passed := true;
                RAISE NOTICE '✓ TEST PASSED: Duplicate draft correctly blocked';
        END;
    ELSE
        RAISE NOTICE 'User has no existing draft. Creating first draft...';
        
        -- Create first draft (should succeed)
        INSERT INTO public.active_drafts (user_id, state, listing_data)
        VALUES (test_user_id, 'draft', '{"test": true}'::jsonb)
        RETURNING id INTO existing_draft_id;
        
        RAISE NOTICE '✓ First draft created: %', existing_draft_id;
        
        -- Try duplicate (should fail)
        BEGIN
            INSERT INTO public.active_drafts (user_id, state, listing_data)
            VALUES (test_user_id, 'draft', '{"test": true}'::jsonb);
            
            RAISE EXCEPTION 'TEST FAILED: Duplicate draft was allowed!';
        EXCEPTION
            WHEN unique_violation THEN
                test_passed := true;
                RAISE NOTICE '✓ TEST PASSED: Duplicate draft correctly blocked';
        END;
        
        -- Cleanup test draft (IMPORTANT!)
        DELETE FROM public.active_drafts WHERE id = existing_draft_id;
        RAISE NOTICE '✓ Test draft cleaned up';
    END IF;
    
    IF test_passed THEN
        RAISE NOTICE '=== CONSTRAINT VERIFICATION SUCCESSFUL ===';
    END IF;
END $$;

-- Alternative: Simple verification query (non-destructive)
-- Just checks if constraint exists
SELECT 
    constraint_name,
    constraint_type
FROM information_schema.table_constraints 
WHERE table_name = 'active_drafts' 
  AND constraint_name = 'active_drafts_user_id_unique';

-- Expected output: 1 row with constraint_name and constraint_type = 'UNIQUE'
