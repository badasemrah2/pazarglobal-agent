"""
Production monitoring checklist and utilities
Provides health checks and metrics collection for critical failure modes
"""
from typing import Dict, List, Any
from datetime import datetime, timedelta
from services import supabase_client, redis_client
from services.logger import get_logger

logger = get_logger(__name__)


async def check_redis_health() -> Dict[str, Any]:
    """
    Check Redis connection health
    
    Returns:
        {
            "healthy": bool,
            "latency_ms": float,
            "error": str (if unhealthy)
        }
    """
    start = datetime.now()
    try:
        if redis_client.disabled:
            return {
                "healthy": True,
                "mode": "in_memory_fallback",
                "warning": "Redis disabled, using volatile memory"
            }
        
        # Ping Redis
        client = await redis_client.get_client()
        await client.ping()
        
        latency = (datetime.now() - start).total_seconds() * 1000
        return {
            "healthy": True,
            "latency_ms": round(latency, 2)
        }
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return {
            "healthy": False,
            "error": str(e)
        }


async def check_draft_orphans(hours: int = 24) -> Dict[str, Any]:
    """
    Check for orphaned drafts (older than X hours with no active session)
    
    Args:
        hours: Age threshold for orphan detection
    
    Returns:
        {
            "orphan_count": int,
            "oldest_orphan_hours": float,
            "sample_orphans": List[dict] (first 5)
        }
    """
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        
        # Query Supabase for old drafts (Supabase client is sync, not async)
        response = supabase_client.client.table("active_drafts") \
            .select("id, user_id, updated_at") \
            .lt("updated_at", cutoff.isoformat()) \
            .execute()
        
        orphans = response.data or []
        
        if not orphans:
            return {"orphan_count": 0}
        
        # Calculate age of oldest
        oldest = min(orphans, key=lambda d: d["updated_at"])
        oldest_age = (datetime.now() - datetime.fromisoformat(oldest["updated_at"])).total_seconds() / 3600
        
        return {
            "orphan_count": len(orphans),
            "oldest_orphan_hours": round(oldest_age, 1),
            "sample_orphans": orphans[:5]
        }
    except Exception as e:
        logger.error(f"Orphan check failed: {e}")
        return {"error": str(e)}


async def check_draft_conflicts(hours: int = 1) -> Dict[str, Any]:
    """
    Check audit_logs for draft_id conflicts in recent time window
    
    Returns:
        {
            "conflict_count": int,
            "affected_users": List[str],
            "recent_conflicts": List[dict]
        }
    """
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        
        response = supabase_client.client.table("audit_logs") \
            .select("id, user_id, metadata, created_at") \
            .eq("action", "draft_conflict_detected") \
            .gte("created_at", cutoff.isoformat()) \
            .execute()
        
        conflicts = response.data or []
        
        if not conflicts:
            return {"conflict_count": 0}
        
        affected_users = list(set(c["user_id"] for c in conflicts if c.get("user_id")))
        
        return {
            "conflict_count": len(conflicts),
            "affected_users": affected_users,
            "recent_conflicts": conflicts[:10]
        }
    except Exception as e:
        logger.error(f"Conflict check failed: {e}")
        return {"error": str(e)}


async def check_fsm_state_distribution() -> Dict[str, Any]:
    """
    Query FSM state distribution from audit_logs
    
    Returns:
        {
            "states": {
                "active": int,
                "parked": int,
                "timeout": int,
                "hesitation_exit": int
            },
            "most_common_state": str
        }
    """
    try:
        # Query last 1000 FSM state changes
        response = supabase_client.client.table("audit_logs") \
            .select("metadata") \
            .eq("action", "fsm_state_change") \
            .order("created_at", desc=True) \
            .limit(1000) \
            .execute()
        
        logs = response.data or []
        
        state_counts = {}
        for log in logs:
            metadata = log.get("metadata", {})
            state = metadata.get("fsm_state")
            if state:
                state_counts[state] = state_counts.get(state, 0) + 1
        
        if not state_counts:
            return {"states": {}, "most_common_state": None}
        
        most_common = max(state_counts, key=state_counts.get)
        
        return {
            "states": state_counts,
            "most_common_state": most_common,
            "total_transitions": sum(state_counts.values())
        }
    except Exception as e:
        logger.error(f"FSM distribution check failed: {e}")
        return {"error": str(e)}


async def check_moderation_api_failures(hours: int = 1) -> Dict[str, Any]:
    """
    Check for moderation API failures (fail-open events)
    
    Returns:
        {
            "failure_count": int,
            "failure_rate": float (percentage),
            "recent_failures": List[dict]
        }
    """
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        
        # Count total moderation checks
        total_response = supabase_client.client.table("audit_logs") \
            .select("id", count="exact") \
            .eq("action", "vision_analysis") \
            .gte("created_at", cutoff.isoformat()) \
            .execute()
        
        total = total_response.count or 0
        
        # Count failures (where metadata contains error)
        failure_response = supabase_client.client.table("audit_logs") \
            .select("id, metadata, created_at") \
            .eq("action", "vision_analysis") \
            .not_.is_("metadata->error", "null") \
            .gte("created_at", cutoff.isoformat()) \
            .execute()
        
        failures = failure_response.data or []
        failure_count = len(failures)
        
        if total == 0:
            return {"failure_count": 0, "failure_rate": 0.0}
        
        failure_rate = (failure_count / total) * 100
        
        return {
            "failure_count": failure_count,
            "total_checks": total,
            "failure_rate": round(failure_rate, 2),
            "recent_failures": failures[:10]
        }
    except Exception as e:
        logger.error(f"Moderation failure check failed: {e}")
        return {"error": str(e)}


async def get_health_dashboard() -> Dict[str, Any]:
    """
    Aggregate all health checks into single dashboard
    
    Returns:
        Complete system health status
    """
    redis = await check_redis_health()
    orphans = await check_draft_orphans()
    conflicts = await check_draft_conflicts()
    fsm_dist = await check_fsm_state_distribution()
    moderation = await check_moderation_api_failures()
    
    # Calculate overall health
    critical_issues = []
    
    if not redis.get("healthy"):
        critical_issues.append("Redis unhealthy")
    
    if orphans.get("orphan_count", 0) > 10:
        critical_issues.append(f"{orphans['orphan_count']} orphaned drafts")
    
    if conflicts.get("conflict_count", 0) > 5:
        critical_issues.append(f"{conflicts['conflict_count']} draft conflicts in last hour")
    
    if moderation.get("failure_rate", 0) > 10:
        critical_issues.append(f"{moderation['failure_rate']}% moderation failures")
    
    overall_healthy = len(critical_issues) == 0
    
    return {
        "healthy": overall_healthy,
        "timestamp": datetime.now().isoformat(),
        "critical_issues": critical_issues,
        "checks": {
            "redis": redis,
            "draft_orphans": orphans,
            "draft_conflicts": conflicts,
            "fsm_states": fsm_dist,
            "moderation_api": moderation
        }
    }


# FastAPI endpoint for health dashboard
from fastapi import APIRouter

monitoring_router = APIRouter(prefix="/monitoring", tags=["monitoring"])


@monitoring_router.get("/health")
async def health_dashboard():
    """Production health dashboard endpoint"""
    return await get_health_dashboard()


@monitoring_router.get("/redis")
async def redis_health():
    """Redis-specific health check"""
    return await check_redis_health()


@monitoring_router.get("/orphans")
async def orphaned_drafts(hours: int = 24):
    """Check for orphaned drafts"""
    return await check_draft_orphans(hours)


@monitoring_router.get("/conflicts")
async def draft_conflicts(hours: int = 1):
    """Check for recent draft conflicts"""
    return await check_draft_conflicts(hours)


@monitoring_router.get("/fsm-distribution")
async def fsm_distribution():
    """FSM state distribution"""
    return await check_fsm_state_distribution()


@monitoring_router.get("/moderation-failures")
async def moderation_failures(hours: int = 1):
    """Moderation API failure rate"""
    return await check_moderation_api_failures(hours)
