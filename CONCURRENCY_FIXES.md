# Concurrency Fixes & Production Monitoring

**Applied:** January 22, 2026  
**Purpose:** Address race conditions and production observability gaps identified in code audit

---

## Changes Applied

### 1. Supabase Schema Fix (CRITICAL)
**File:** `migrations/001_fix_draft_uniqueness.sql`

**Problem:** 
- No `UNIQUE(user_id)` constraint on `active_drafts` table
- Parallel requests could create duplicate drafts

**Solution:**
```sql
ALTER TABLE public.active_drafts 
ADD CONSTRAINT active_drafts_user_id_unique UNIQUE (user_id);
```

**Migration includes:**
- Duplicate cleanup (keeps newest draft)
- Index for faster lookups
- Foreign key cascade delete
- Automated verification test

**Apply:**
```bash
# Supabase Dashboard → SQL Editor → Paste migration SQL → Run
```

---

### 2. Redis Atomic Operations
**File:** `services/redis_atomic.py`

**Problem:**
- `set_session()` uses read-modify-write (not atomic)
- Parallel updates → lost writes

**Solution:**
- Lua scripts for atomic field updates
- Distributed locking for critical sections
- In-memory fallback compatibility

**Usage:**
```python
from services.redis_atomic import get_atomic_ops

atomic = get_atomic_ops(redis_client)

# Atomic single field update
await atomic.atomic_update_field(session_id, "locked_intent", "create_listing")

# Atomic multi-field merge
await atomic.atomic_merge_updates(session_id, {
    "locked_intent": "create_listing",
    "fsm_state": "active",
    "last_user_at": datetime.now().isoformat()
})
```

**Benefits:**
- Prevents lost updates
- No race conditions
- Backwards compatible (graceful fallback)

---

### 3. Concurrency Tests
**File:** `test_concurrency.py`

**Coverage:**
- `test_parallel_draft_creation_prevents_duplicates()` - Validates UNIQUE constraint
- `test_concurrent_session_updates_preserve_state()` - Documents lost update problem
- `test_redis_restart_orphans_active_drafts()` - Redis volatility test
- `test_moderation_api_timeout_allows_content()` - Vision safety fail-open
- `test_moderation_api_rate_limit_handling()` - Rate limit behavior

**Run:**
```bash
pytest test_concurrency.py -v
```

**Expected:**
- Draft uniqueness test: PASS (after migration)
- Session update test: FAIL (documents race condition without atomic ops)
- Other tests: PASS (verify fail-open behavior)

---

### 4. Production Monitoring Dashboard
**File:** `services/monitoring.py`

**Endpoints:**
```
GET /monitoring/health           - Aggregate health dashboard
GET /monitoring/redis            - Redis connection health
GET /monitoring/orphans          - Orphaned draft detection
GET /monitoring/conflicts        - Draft conflict events
GET /monitoring/fsm-distribution - FSM state statistics
GET /monitoring/moderation-failures - Vision API failure rate
```

**Alerts to configure:**
- Redis down → fallback mode active
- Orphan count > 10 → cleanup needed
- Draft conflicts > 5/hour → concurrency issue
- Moderation failure rate > 10% → API degradation

**Usage:**
```bash
# Local
curl http://localhost:8000/monitoring/health

# Production (Railway)
curl https://your-app.railway.app/monitoring/health
```

**Response:**
```json
{
  "healthy": true,
  "timestamp": "2026-01-22T10:00:00Z",
  "critical_issues": [],
  "checks": {
    "redis": {"healthy": true, "latency_ms": 1.2},
    "draft_orphans": {"orphan_count": 0},
    "draft_conflicts": {"conflict_count": 0},
    "fsm_states": {"states": {"active": 850, "parked": 120}},
    "moderation_api": {"failure_rate": 0.5}
  }
}
```

---

### 5. Documentation Updates
**File:** `.github/copilot-instructions.md`

**Added sections:**
- ⚠️ Known Failure Modes & Constraints
- 🔒 Invariants (Must Be Preserved)
- 🧪 Testing Gaps & Recommendations
- 📋 Required Schema Fixes
- 🚨 Production Monitoring Checklist

**Purpose:**
- Guide AI agents with real constraints
- Document failure modes (not just happy paths)
- Provide actionable debugging steps

---

## Deployment Steps

### Step 1: Apply Supabase Migration
```bash
# 1. Open Supabase Dashboard
# 2. Navigate to SQL Editor
# 3. Copy contents of migrations/001_fix_draft_uniqueness.sql
# 4. Run migration
# 5. Verify output: "✓ Constraint working: Duplicate draft blocked as expected"
```

### Step 2: Update Agent Code (Optional)
To use atomic operations, replace direct `redis_client` calls:

**Before:**
```python
session = await redis_client.get_session(session_id)
session["locked_intent"] = "create_listing"
await redis_client.set_session(session_id, session)
```

**After:**
```python
from services.redis_atomic import get_atomic_ops
atomic = get_atomic_ops(redis_client)
await atomic.atomic_update_field(session_id, "locked_intent", "create_listing")
```

### Step 3: Deploy to Railway
```bash
git add .
git commit -m "feat: add concurrency fixes and monitoring"
git push origin main

# Railway auto-deploys on push
```

### Step 4: Configure Alerts
Monitor these endpoints for production health:
- Set up Railway metrics dashboard
- Configure Slack/email alerts for `/monitoring/health` failures
- Query `audit_logs` daily for conflict trends

---

## Verification

### Verify Migration Applied
```sql
-- Should return 1 row
SELECT constraint_name 
FROM information_schema.table_constraints 
WHERE table_name = 'active_drafts' 
  AND constraint_name = 'active_drafts_user_id_unique';
```

### Verify Monitoring Active
```bash
curl http://localhost:8000/monitoring/health | jq .
```

### Verify Tests Pass
```bash
pytest test_concurrency.py::test_parallel_draft_creation_prevents_duplicates -v
```

---

## Rollback Plan

### If Migration Causes Issues:
```sql
-- Remove constraint
ALTER TABLE public.active_drafts 
DROP CONSTRAINT IF EXISTS active_drafts_user_id_unique;

-- Remove index
DROP INDEX IF EXISTS idx_active_drafts_user_id;
```

### If Monitoring Causes Issues:
Comment out in `main.py`:
```python
# app.include_router(monitoring_router)
```

---

## Next Steps (Future Work)

1. **Implement atomic operations system-wide**
   - Replace all `persist_session_state()` calls with atomic variants
   - Add integration tests with real Redis

2. **Add retry logic for transient failures**
   - Supabase timeout → exponential backoff
   - OpenAI rate limit → queue + retry

3. **Implement draft rollback on publish failure**
   - Use Supabase transactions
   - Compensating action pattern

4. **Add load testing**
   - Simulate 100 concurrent users
   - Measure Redis connection pool exhaustion
   - Test Railway autoscaling behavior

---

## Questions?

Refer to:
- `.github/copilot-instructions.md` - Full system documentation
- `FSM_TELEMETRY_DEBUG.md` - FSM debugging guide
- Railway logs for production errors
