# FSM Telemetry Debugging Guide

**Sprint 3 Implementation - January 2026**

## Overview

The FSM (Finite State Machine) telemetry system tracks all state transitions, user interactions, and timeout events in the webchat flow. This document explains how to use telemetry for debugging production issues.

## Architecture

### Telemetry Layers

1. **In-Memory Logging** (`logger.info`, `logger.debug`)
   - Instant visibility during development
   - Structured format for easy parsing
   - Available in Railway logs

2. **Persistent Storage** (`audit_logs` table)
   - Long-term analysis
   - Query historical patterns
   - Correlate with user reports

### Session State Fields

```python
session = {
    # Identity
    "user_id": str,
    "session_id": str,
    
    # Intent tracking
    "intent": str | None,
    "locked_intent": str | None,
    "parked_intent": str | None,
    
    # FSM state
    "fsm_state": "active" | "parked" | "timeout" | "hesitation_exit",
    "fsm_state_reason": str | None,
    "fsm_state_updated_at": str,  # ISO8601
    "fsm_state_intent": str | None,
    
    # Timestamps
    "last_user_at": str,  # ISO8601
    "last_bot_at": str,   # ISO8601
    
    # Draft context
    "active_draft_id": str | None,
}
```

## Log Formats

### State Transition Logs

```
INFO | FSM state transition: active → parked (reason=inactivity, intent=create_listing)
```

**Fields:**
- Previous state
- New state
- Reason (inactivity | composer_timeout | user_hesitation | cancel_from_parked | resume_command)
- Intent being worked on

### Telemetry Event Logs

```
INFO | FSM telemetry: event=parked, session=abc12345..., detail={'inactivity_seconds': 660.0, 'parked_intent': 'create_listing'}
```

**Events:**
- `parked` - User inactive for 10+ minutes
- `timeout` - ComposerAgent exceeded 45s
- `resumed` - User said "devam" to restore flow
- `parked_cancel` - User said "iptal" to reset parked flow
- `hesitation_exit` - User showed uncertainty signals
- `intent_lock` - Intent changed (create_listing, search_listings, publish_or_delete)

### Inactivity Check Logs

```
DEBUG | Session abc12345... inactivity: 660.0s, locked=create_listing, fsm_state=active
```

**Fields:**
- Session ID (truncated for readability)
- Inactivity seconds (time since `last_user_at`)
- Current locked intent
- Current FSM state

### Parked/Timeout State Logs

```
INFO | Session abc12345... in parked state, awaiting resume/cancel
```

## Database Schema

### audit_logs Table

```sql
CREATE TABLE public.audit_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID,
  phone TEXT,
  action TEXT,  -- 'fsm_event' for FSM telemetry
  resource_type TEXT,  -- 'session'
  resource_id TEXT,  -- session_id
  source TEXT,
  ip_address TEXT,
  user_agent TEXT,
  request_data JSONB,
  response_status INTEGER,
  error_message TEXT,
  metadata JSONB,  -- FSM event details here
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Metadata Structure (for FSM events)

```json
{
  "event": "parked",
  "session_id": "abc123...",
  "intent": "create_listing",
  "locked_intent": "create_listing",
  "fsm_state": "parked",
  "fsm_state_reason": "inactivity",
  "inactivity_seconds": 660.0,
  "parked_intent": "create_listing"
}
```

## Debugging Scenarios

### 1. User Reports "Bot Keeps Asking Same Question"

**Query:**
```sql
SELECT 
  created_at,
  metadata->>'event' as event,
  metadata->>'session_id' as session_id,
  metadata->>'locked_intent' as intent,
  metadata->>'fsm_state' as state,
  metadata->>'fsm_state_reason' as reason
FROM audit_logs
WHERE 
  action = 'fsm_event'
  AND metadata->>'session_id' LIKE 'abc%'
ORDER BY created_at DESC
LIMIT 50;
```

**Look for:**
- Multiple `intent_lock` events without progress
- Missing `hesitation_exit` when user showed uncertainty
- `parked` event that should have triggered but didn't

### 2. User Reports "Bot Stopped Responding"

**Check logs for:**
```
WARNING | ComposerAgent timeout after 45s for session abc...
INFO | FSM state transition: active → timeout (reason=composer_timeout, intent=create_listing)
```

**Query audit_logs:**
```sql
SELECT *
FROM audit_logs
WHERE 
  action = 'fsm_event'
  AND metadata->>'event' = 'timeout'
  AND created_at > NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC;
```

**Mitigation:**
- Check if OpenAI API is slow
- Verify network connectivity
- Consider increasing `FSM_COMPOSER_TIMEOUT_SECONDS`

### 3. User Says "I Wrote 'devam' But Nothing Happened"

**Check logs for:**
```
INFO | Session abc... in parked state, awaiting resume/cancel
INFO | FSM telemetry: event=resumed, session=abc..., detail={'restored_intent': 'create_listing'}
```

**Query:**
```sql
SELECT 
  created_at,
  metadata->>'event' as event,
  metadata->>'parked_intent' as parked_intent,
  metadata->>'restored_intent' as restored_intent
FROM audit_logs
WHERE 
  action = 'fsm_event'
  AND metadata->>'event' IN ('parked', 'resumed', 'parked_cancel')
  AND metadata->>'session_id' LIKE 'abc%'
ORDER BY created_at;
```

**Possible issues:**
- Typo in keyword (not in `RESUME_KEYWORDS`)
- Session expired between park and resume
- DB connection lost during restore

### 4. User Inactivity Pattern Analysis

**Query to find users who get parked frequently:**
```sql
SELECT 
  metadata->>'session_id' as session_id,
  COUNT(*) as park_count,
  AVG((metadata->>'inactivity_seconds')::float) as avg_inactivity
FROM audit_logs
WHERE 
  action = 'fsm_event'
  AND metadata->>'event' = 'parked'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY metadata->>'session_id'
HAVING COUNT(*) > 3
ORDER BY park_count DESC;
```

**Insights:**
- If many users get parked, consider UX hints
- If avg_inactivity is close to 600s, maybe reduce timeout
- If users never resume, add more guidance in park message

### 5. ComposerAgent Timeout Trends

**Query:**
```sql
SELECT 
  DATE_TRUNC('hour', created_at) as hour,
  COUNT(*) as timeout_count,
  AVG((metadata->'detail'->>'inactivity_seconds')::float) as avg_wait
FROM audit_logs
WHERE 
  action = 'fsm_event'
  AND metadata->>'event' = 'timeout'
  AND metadata->'detail'->>'stage' = 'composer'
GROUP BY hour
ORDER BY hour DESC
LIMIT 24;
```

**Correlate with:**
- OpenAI API status page
- Railway resource metrics
- Concurrent user load

## Configuration Constants

```python
# api/webchat.py
FSM_PARK_TIMEOUT_SECONDS = 10 * 60  # 600s = 10 minutes
FSM_COMPOSER_TIMEOUT_SECONDS = 45   # 45 seconds
RESUME_KEYWORDS = {"devam", "kaldığımız yerden", "kaldigimiz yerden", "resume", "continue"}
```

**Tuning guidelines:**
- `FSM_PARK_TIMEOUT_SECONDS`: Increase if users report premature parking; decrease if stale sessions are a problem
- `FSM_COMPOSER_TIMEOUT_SECONDS`: Increase if OpenAI is consistently slow; decrease to fail faster
- `RESUME_KEYWORDS`: Add localized variants based on user feedback

## Log Level Configuration

### Development (local)
```python
# config/settings.py
LOG_LEVEL = "DEBUG"
```

**Output:**
- All inactivity checks
- All telemetry emit attempts
- Session state snapshots

### Production (Railway)
```python
LOG_LEVEL = "INFO"
```

**Output:**
- State transitions
- Telemetry events
- Parked/timeout warnings

### Critical Only
```python
LOG_LEVEL = "WARNING"
```

**Output:**
- Only errors and timeouts

## Telemetry Health Checks

### 1. Verify Telemetry is Working

**Test:**
```bash
# Run FSM tests with verbose logging
pytest test_fsm_sprint3.py -v -s
```

**Expected output:**
```
INFO | FSM state transition: active → parked
INFO | FSM telemetry: event=parked
DEBUG | FSM telemetry persisted to audit_logs: parked
```

### 2. Check Audit Log Insertion

**Query:**
```sql
SELECT COUNT(*) 
FROM audit_logs 
WHERE 
  action = 'fsm_event'
  AND created_at > NOW() - INTERVAL '1 hour';
```

**Expected:**
- Non-zero count if FSM events occurred
- If zero, check Supabase permissions or `log_action()` implementation

### 3. Validate Session Timestamps

**Check logs for:**
```
DEBUG | Session abc... inactivity: 0.1s, locked=None, fsm_state=active
```

**On every user message:**
- `last_user_at` should update
- `last_bot_at` should update on response

## Fail-Safe Design

### Telemetry Never Breaks Flow

```python
async def _record_fsm_event(...):
    try:
        # Log and persist
        ...
    except Exception as e:
        # Never raise, only log
        logger.warning(f"FSM telemetry emit failed for {event}: {e}")
```

**Guarantees:**
- User experience is never blocked by telemetry failures
- Degraded mode: in-memory logs still available
- DB connection loss doesn't stop conversations

### Retry Strategy

Currently: **No retry** (fail-fast)

**Rationale:**
- Telemetry is diagnostic, not transactional
- Retrying would add latency to every request
- Missing one event is acceptable vs. delaying user response

**Future enhancement:**
- Background queue for telemetry events
- Batch insertion every 10s
- Only for high-value events (parked, timeout)

## Common Pitfalls

### 1. Session ID Truncation in Logs

**Why:** Full UUIDs clutter logs

**Solution:**
```python
logger.info(f"Session {session_id[:8]}...")
```

**Note:** Full session_id is still in `audit_logs.resource_id`

### 2. Timestamps Not Updating

**Symptom:** All users get parked immediately

**Cause:** `last_user_at` not initialized

**Fix:** Ensure session defaults include:
```python
"last_user_at": _utc_now_iso()
```

### 3. Telemetry Missing in Tests

**Symptom:** `log_action()` not called

**Cause:** Fake Supabase client in tests doesn't implement it

**Fix:**
```python
class FakeSupabase:
    async def log_action(self, **kwargs) -> bool:
        self.log_calls.append(kwargs)
        return True
```

### 4. Inactivity Calculation Off by Hours

**Symptom:** Park timeout triggers at wrong times

**Cause:** Timezone mismatch (naive vs. aware datetime)

**Fix:** Always use:
```python
datetime.now(timezone.utc).isoformat()
```

## Performance Impact

### Overhead per Request

- In-memory logging: ~0.1ms
- Audit log insert: ~5-10ms (async, non-blocking)
- Session state update: ~2ms (Redis/in-memory)

**Total:** ~10ms per request (< 1% of typical 1-2s response time)

### Storage Growth

- ~300 bytes per FSM event
- ~1000 events/day for 100 active users
- ~9 MB/month

**Retention policy:**
```sql
-- Clean up old telemetry (run weekly)
DELETE FROM audit_logs
WHERE 
  action = 'fsm_event'
  AND created_at < NOW() - INTERVAL '90 days';
```

## Alerting Recommendations

### Critical Alerts

1. **Timeout spike:**
   ```sql
   SELECT COUNT(*) > 10 FROM audit_logs
   WHERE action = 'fsm_event'
     AND metadata->>'event' = 'timeout'
     AND created_at > NOW() - INTERVAL '5 minutes';
   ```

2. **Telemetry failure:**
   ```bash
   grep "FSM telemetry emit failed" railway.log | wc -l
   ```

### Warning Alerts

1. **High park rate:**
   ```sql
   SELECT COUNT(*) > 50 FROM audit_logs
   WHERE action = 'fsm_event'
     AND metadata->>'event' = 'parked'
     AND created_at > NOW() - INTERVAL '1 hour';
   ```

2. **Low resume rate:**
   ```sql
   WITH parks AS (
     SELECT COUNT(*) as park_count FROM audit_logs
     WHERE action = 'fsm_event' AND metadata->>'event' = 'parked'
       AND created_at > NOW() - INTERVAL '1 day'
   ),
   resumes AS (
     SELECT COUNT(*) as resume_count FROM audit_logs
     WHERE action = 'fsm_event' AND metadata->>'event' = 'resumed'
       AND created_at > NOW() - INTERVAL '1 day'
   )
   SELECT 
     park_count,
     resume_count,
     (resume_count::float / NULLIF(park_count, 0)) * 100 as resume_rate_pct
   FROM parks, resumes;
   ```
   Alert if resume_rate < 30%

## Troubleshooting Checklist

- [ ] Check Railway logs for FSM transitions
- [ ] Query `audit_logs` for session history
- [ ] Verify `last_user_at` is updating
- [ ] Confirm `locked_intent` matches user expectation
- [ ] Check if `fsm_state` stuck in non-active state
- [ ] Look for timeout events in last 24h
- [ ] Verify resume keywords are recognized
- [ ] Test telemetry with `pytest test_fsm_sprint3.py -v -s`
- [ ] Check Supabase connection health
- [ ] Review OpenAI API latency

## Future Enhancements

1. **Session replay:** Store full message history in metadata
2. **User journey visualization:** Build timeline from audit_logs
3. **Predictive parking:** ML model to predict when user will abandon
4. **Smart resume:** Auto-fill context when resuming parked flow
5. **Telemetry dashboard:** Real-time FSM state distribution chart

---

**Last Updated:** Sprint 3 - January 6, 2026  
**Test Coverage:** test_fsm_sprint3.py (5 tests passing)
