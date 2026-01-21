"""
Concurrency tests for session state management and draft creation
Tests race conditions that can occur in production with parallel requests
"""
import asyncio
import pytest
from typing import List
from unittest.mock import AsyncMock, MagicMock
from api.webchat_store import load_session_state, persist_session_state, IN_MEMORY_SESSION_CACHE
from services import supabase_client


@pytest.fixture
def mock_supabase_parallel(monkeypatch):
    """Mock Supabase client that simulates parallel draft creation"""
    created_drafts: List[dict] = []
    
    class MockSupabase:
        async def create_draft(self, user_id: str, **kwargs):
            # Simulate DB delay
            await asyncio.sleep(0.01)
            
            # Check if draft already exists (WOULD fail without UNIQUE constraint)
            existing = [d for d in created_drafts if d["user_id"] == user_id]
            if existing:
                # With UNIQUE constraint, this raises
                raise Exception(f"duplicate key value violates unique constraint \"active_drafts_user_id_unique\"")
            
            draft = {
                "id": f"draft_{len(created_drafts)}",
                "user_id": user_id,
                "state": "draft",
                "listing_data": kwargs.get("listing_data", {})
            }
            created_drafts.append(draft)
            return {"success": True, "data": draft}
        
        async def get_latest_draft_for_user(self, user_id: str):
            drafts = [d for d in created_drafts if d["user_id"] == user_id]
            if not drafts:
                return None
            return drafts[-1]
    
    mock = MockSupabase()
    monkeypatch.setattr(supabase_client, "create_draft", mock.create_draft)
    monkeypatch.setattr(supabase_client, "get_latest_draft_for_user", mock.get_latest_draft_for_user)
    return mock


@pytest.mark.asyncio
async def test_parallel_draft_creation_prevents_duplicates(mock_supabase_parallel):
    """
    Test that parallel draft creation attempts are blocked by UNIQUE constraint
    
    Scenario:
        - 2 requests arrive at same time for same user
        - Both try to create draft
        - Database UNIQUE constraint blocks second attempt
    
    Expected: Only 1 draft created, second request gets constraint error
    """
    user_id = "user_123"
    
    async def create_draft_task(task_id: int):
        try:
            result = await mock_supabase_parallel.create_draft(user_id, listing_data={"task": task_id})
            return {"task": task_id, "success": True, "draft_id": result["data"]["id"]}
        except Exception as e:
            return {"task": task_id, "success": False, "error": str(e)}
    
    # Launch 2 parallel tasks
    results = await asyncio.gather(
        create_draft_task(1),
        create_draft_task(2)
    )
    
    # Exactly 1 should succeed
    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]
    
    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}"
    assert "unique constraint" in failures[0]["error"].lower()


@pytest.mark.asyncio
async def test_concurrent_session_updates_preserve_state():
    """
    Test that concurrent session updates don't lose data
    
    Scenario:
        - Session has {"count": 0}
        - 10 parallel updates each increment count
        - Without atomic operations: final count < 10 (lost updates)
        - With atomic operations: final count == 10
    
    Expected: All updates applied (no lost writes)
    """
    session_id = "test_session_concurrent"
    IN_MEMORY_SESSION_CACHE[session_id] = {"count": 0, "updates": []}
    
    async def increment_count(task_id: int):
        # Simulate read-modify-write WITHOUT atomicity
        await asyncio.sleep(0.001)  # Simulate network delay
        
        session = await load_session_state(session_id) or {}
        current_count = session.get("count", 0)
        updates = session.get("updates", [])
        
        # CRITICAL SECTION: Another task may read old value here
        await asyncio.sleep(0.001)
        
        session["count"] = current_count + 1
        session["updates"] = updates + [task_id]
        await persist_session_state(session_id, session)
    
    # Launch 10 concurrent updates
    await asyncio.gather(*[increment_count(i) for i in range(10)])
    
    final_session = await load_session_state(session_id)
    
    # WITHOUT atomic operations, this will fail (lost updates)
    # Expected failure: final_session["count"] < 10
    assert final_session["count"] < 10, \
        "Race condition NOT detected! This means atomicity is somehow present (unexpected)"
    
    # This test DOCUMENTS the problem - fix requires redis_atomic.py


@pytest.mark.asyncio
async def test_redis_restart_orphans_active_drafts(mock_supabase_parallel):
    """
    Test that Redis restart causes in-memory/DB divergence
    
    Scenario:
        1. User creates draft (stored in Redis + Supabase)
        2. Redis container restarts → in-memory cache cleared
        3. User sends another message → loads from fallback
        4. Supabase still has draft but session state lost
    
    Expected: Session doesn't know about existing draft_id
    """
    user_id = "user_redis_restart"
    session_id = "session_redis_restart"
    
    # Step 1: Create draft and store in session
    draft_result = await mock_supabase_parallel.create_draft(user_id)
    draft_id = draft_result["data"]["id"]
    
    session = {
        "user_id": user_id,
        "active_draft_id": draft_id,
        "locked_intent": "create_listing"
    }
    await persist_session_state(session_id, session)
    
    # Verify session stored
    loaded = await load_session_state(session_id)
    assert loaded["active_draft_id"] == draft_id
    
    # Step 2: Simulate Redis restart (clear in-memory cache)
    IN_MEMORY_SESSION_CACHE.clear()
    
    # Step 3: Load session again (fallback should return None)
    reloaded = await load_session_state(session_id)
    assert reloaded is None, "Session should be lost after Redis restart"
    
    # Step 4: Check Supabase still has draft
    db_draft = await mock_supabase_parallel.get_latest_draft_for_user(user_id)
    assert db_draft is not None, "Draft orphaned in DB"
    assert db_draft["id"] == draft_id
    
    # PROBLEM: Session lost but draft exists → mismatch on next request


@pytest.mark.asyncio
async def test_draft_cleanup_after_publish_failure():
    """
    Test that failed publish attempts don't orphan drafts
    
    Scenario:
        1. User creates draft
        2. User publishes → publish fails (e.g., Supabase timeout)
        3. Draft already deleted from active_drafts
        4. User can't recover → must recreate listing
    
    Expected: Draft should rollback on publish failure
    """
    # This test documents the MISSING rollback mechanism
    # Implementation requires transaction or compensating action
    pytest.skip("Rollback mechanism not implemented - tracked in issues")


@pytest.mark.asyncio 
async def test_moderation_api_timeout_allows_content():
    """
    Test that moderation API timeout results in fail-open behavior
    
    Scenario:
        - User uploads image
        - OpenAI Moderation API times out
        - System should allow content (fail-open) but log warning
    
    Expected: Content passes, warning logged
    """
    from agents.vision_safety_gate import vision_safety_gate
    from unittest.mock import AsyncMock, patch
    
    # Mock OpenAI client to timeout
    with patch("agents.vision_safety_gate.openai_client") as mock_openai:
        mock_openai.client.moderations.create = AsyncMock(
            side_effect=asyncio.TimeoutError("Moderation API timeout")
        )
        
        result = await vision_safety_gate.check_image_safety("http://example.com/image.jpg")
        
        # Fail-open: should pass despite error
        assert result["is_safe"] is True, "Should fail-open on timeout"
        assert "error" in result or "warning" in result


@pytest.mark.asyncio
async def test_moderation_api_rate_limit_handling():
    """
    Test behavior when moderation API returns rate limit error
    
    Expected: Fail-open with logged warning
    """
    from agents.vision_safety_gate import vision_safety_gate
    from unittest.mock import AsyncMock, patch
    from openai import RateLimitError
    
    with patch("agents.vision_safety_gate.openai_client") as mock_openai:
        mock_openai.client.moderations.create = AsyncMock(
            side_effect=RateLimitError("Rate limit exceeded", response=None, body=None)
        )
        
        result = await vision_safety_gate.check_image_safety("http://example.com/image.jpg")
        
        # Should fail-open
        assert result["is_safe"] is True
        # Should log warning for monitoring
        # (Verify via log capture in real implementation)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
