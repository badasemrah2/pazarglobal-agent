"""services.redis_client

Session state store with caching, rate limiting, and metrics.

This project can run without a Redis instance (e.g. local dev). In that case,
we keep a lightweight in-memory fallback so session state (intent, drafts,
pending media) does not reset every request.
"""

from __future__ import annotations

from typing import Optional, Dict, Any
import json
import os
import hashlib
from loguru import logger

# redis is intentionally not imported to avoid connection attempts when disabled

# Redis TTL configuration (from environment with defaults)
REDIS_TTL_SESSION = int(os.getenv("REDIS_TTL_SESSION", 900))  # 15 minutes
REDIS_TTL_CACHE = int(os.getenv("REDIS_TTL_CACHE", 3600))  # 1 hour
REDIS_TTL_RATE = int(os.getenv("REDIS_TTL_RATE", 60))  # 1 minute
REDIS_TTL_CONVERSATION = int(os.getenv("REDIS_TTL_CONVERSATION", 1800))  # 30 minutes


_IN_MEMORY_SESSIONS: Dict[str, Dict[str, Any]] = {}
_IN_MEMORY_MESSAGES: Dict[str, list] = {}
_IN_MEMORY_CACHE: Dict[str, Any] = {}
_IN_MEMORY_METRICS: Dict[str, int] = {}
_IN_MEMORY_WA_AUTH: Dict[str, str] = {}


class RedisClient:
    """Redis client for session state management"""
    
    def __init__(self):
        self._client: Optional[Any] = None  # type: ignore
        # Auto-detect Redis availability from environment
        redis_url = os.getenv("REDIS_URL", "")
        self.disabled = not redis_url or redis_url == "redis://localhost:6379"
        if not self.disabled:
            logger.info(f"✅ Redis enabled: {redis_url.split('@')[-1]}")
        else:
            logger.warning("⚠️ Redis disabled - using in-memory fallback")
    
    async def get_client(self) -> Optional[Any]:
        """Get or create Redis client"""
        if self.disabled:
            return None
        if self._client is None:
            import redis.asyncio as redis  # local import to avoid module load when disabled
            from config import settings
            self._client = await redis.from_url(
                settings.redis_url,
                db=settings.redis_db,
                decode_responses=True
            )
        return self._client
    
    async def close(self):
        """Close Redis connection"""
        if self.disabled:
            return
        if self._client:
            await self._client.close()
    
    # Session State Management
    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session state"""
        try:
            if self.disabled:
                data = _IN_MEMORY_SESSIONS.get(session_id)
                return dict(data) if isinstance(data, dict) else None
            client = await self.get_client()
            data = await client.get(f"session:{session_id}")
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"Error getting session: {e}")
            return None
    
    async def set_session(self, session_id: str, data: Dict[str, Any], ttl: int = 86400) -> bool:
        """Set session state with TTL (default 24 hours)"""
        try:
            if self.disabled:
                _IN_MEMORY_SESSIONS[session_id] = dict(data)
                return True
            client = await self.get_client()
            await client.setex(
                f"session:{session_id}",
                ttl,
                json.dumps(data)
            )
            return True
        except Exception as e:
            logger.error(f"Error setting session: {e}")
            return False

    # ========== WhatsApp Auth Session ==========

    def _wa_auth_key(self, phone: str) -> str:
        normalized = (phone or "").strip()
        if normalized.lower().startswith("whatsapp:"):
            normalized = normalized.split(":", 1)[1]
        return f"wa_auth:{normalized}"

    async def set_whatsapp_auth(self, phone: str, user_id: str, ttl: int = 600) -> bool:
        """Cache WhatsApp phone->user_id mapping for the active PIN session."""
        try:
            key = self._wa_auth_key(phone)
            if self.disabled:
                _IN_MEMORY_WA_AUTH[key] = str(user_id)
                return True
            client = await self.get_client()
            await client.setex(key, ttl, str(user_id))
            return True
        except Exception as e:
            logger.error(f"Error setting WhatsApp auth: {e}")
            return False

    async def get_whatsapp_auth(self, phone: str) -> Optional[str]:
        """Return cached user_id for a WhatsApp phone if session is active."""
        try:
            key = self._wa_auth_key(phone)
            if self.disabled:
                return _IN_MEMORY_WA_AUTH.get(key)
            client = await self.get_client()
            return await client.get(key)
        except Exception as e:
            logger.error(f"Error getting WhatsApp auth: {e}")
            return None

    async def extend_whatsapp_auth(self, phone: str, ttl: int = 600) -> bool:
        """Sliding expiration for WhatsApp auth mapping."""
        try:
            key = self._wa_auth_key(phone)
            if self.disabled:
                return True
            client = await self.get_client()
            await client.expire(key, ttl)
            return True
        except Exception as e:
            logger.error(f"Error extending WhatsApp auth: {e}")
            return False

    async def delete_whatsapp_auth(self, phone: str) -> bool:
        try:
            key = self._wa_auth_key(phone)
            if self.disabled:
                _IN_MEMORY_WA_AUTH.pop(key, None)
                return True
            client = await self.get_client()
            await client.delete(key)
            return True
        except Exception as e:
            logger.error(f"Error deleting WhatsApp auth: {e}")
            return False
    
    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Update session state"""
        try:
            if self.disabled:
                session = _IN_MEMORY_SESSIONS.get(session_id) or {}
                if not isinstance(session, dict):
                    session = {}
                session.update(updates)
                _IN_MEMORY_SESSIONS[session_id] = session
                return True
            session = await self.get_session(session_id) or {}
            session.update(updates)
            return await self.set_session(session_id, session)
        except Exception as e:
            logger.error(f"Error updating session: {e}")
            return False
    
    async def delete_session(self, session_id: str) -> bool:
        """Delete session state"""
        try:
            if self.disabled:
                _IN_MEMORY_SESSIONS.pop(session_id, None)
                _IN_MEMORY_MESSAGES.pop(session_id, None)
                return True
            client = await self.get_client()
            await client.delete(f"session:{session_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting session: {e}")
            return False
    
    # Intent Management
    async def set_intent(self, session_id: str, intent: str) -> bool:
        """Set session intent"""
        return await self.update_session(session_id, {"intent": intent})
    
    async def get_intent(self, session_id: str) -> Optional[str]:
        """Get session intent"""
        session = await self.get_session(session_id)
        return session.get("intent") if session else None
    
    # Draft Management
    async def set_active_draft(self, session_id: str, draft_id: str) -> bool:
        """Set active draft for session"""
        return await self.update_session(session_id, {"active_draft_id": draft_id})
    
    async def get_active_draft(self, session_id: str) -> Optional[str]:
        """Get active draft ID for session"""
        session = await self.get_session(session_id)
        return session.get("active_draft_id") if session else None
    
    # Rate Limiting
    async def check_rate_limit(self, user_id: str, limit: int, window: int) -> bool:
        """Check if user is within rate limit"""
        try:
            if self.disabled:
                return True
            client = await self.get_client()
            key = f"ratelimit:{user_id}"
            count = await client.incr(key)
            
            if count == 1:
                await client.expire(key, window)
            
            return count <= limit
        except Exception as e:
            logger.error(f"Error checking rate limit: {e}")
            return True  # Fail open
    
    # Message History (optional)
    async def add_message(self, session_id: str, message: Dict[str, Any]) -> bool:
        """Add message to session history"""
        try:
            if self.disabled:
                history = _IN_MEMORY_MESSAGES.get(session_id) or []
                history.insert(0, message)
                _IN_MEMORY_MESSAGES[session_id] = history[:100]
                return True
            client = await self.get_client()
            await client.lpush(
                f"messages:{session_id}",
                json.dumps(message)
            )
            await client.ltrim(f"messages:{session_id}", 0, 99)  # Keep last 100 messages
            await client.expire(f"messages:{session_id}", 86400)  # 24 hour TTL
            return True
        except Exception as e:
            logger.error(f"Error adding message: {e}")
            return False
    
    async def get_messages(self, session_id: str, limit: int = 10) -> list:
        """Get recent messages from session"""
        try:
            if self.disabled:
                history = _IN_MEMORY_MESSAGES.get(session_id) or []
                return history[:limit]
            client = await self.get_client()
            messages = await client.lrange(f"messages:{session_id}", 0, limit - 1)
            return [json.loads(msg) for msg in messages]
        except Exception as e:
            logger.error(f"Error getting messages: {e}")
            return []

    # ========== Search Cache ==========

    def _generate_cache_key(self, query: str, filters: Optional[Dict[str, Any]] = None) -> str:
        """Generate deterministic cache key from query and filters"""
        cache_data = {"query": query.lower().strip()}
        if filters:
            cache_data.update(filters)
        cache_str = json.dumps(cache_data, sort_keys=True)
        return hashlib.md5(cache_str.encode()).hexdigest()

    async def get_search_cache(self, query: str, filters: Optional[Dict[str, Any]] = None) -> Optional[list]:
        """Get cached search results"""
        try:
            cache_key = self._generate_cache_key(query, filters)

            if self.disabled:
                result = _IN_MEMORY_CACHE.get(cache_key)
                if result:
                    _IN_MEMORY_METRICS["cache_hit"] = _IN_MEMORY_METRICS.get("cache_hit", 0) + 1
                else:
                    _IN_MEMORY_METRICS["cache_miss"] = _IN_MEMORY_METRICS.get("cache_miss", 0) + 1
                return result

            client = await self.get_client()
            data = await client.get(f"search:{cache_key}")

            if data:
                await self.incr_metric("cache_hit")
                return json.loads(data)
            await self.incr_metric("cache_miss")
            return None
        except Exception as e:
            logger.error(f"Error getting search cache: {e}")
            await self.incr_metric("cache_miss")
            return None

    async def clear_search_cache(self) -> int:
        """Clear all search cache entries. Returns number of keys deleted."""
        try:
            if self.disabled:
                count = len([k for k in _IN_MEMORY_CACHE.keys() if k.startswith("search:")])
                _IN_MEMORY_CACHE.clear()
                logger.info(f"Cleared {count} in-memory search cache entries")
                return count

            client = await self.get_client()
            keys = await client.keys("search:*")
            if keys:
                count = await client.delete(*keys)
                logger.info(f"Cleared {count} Redis search cache entries")
                return count
            return 0
        except Exception as e:
            logger.error(f"Error clearing search cache: {e}")
            return 0

    async def set_search_cache(self, query: str, results: list, filters: Optional[Dict[str, Any]] = None) -> bool:
        """Cache search results"""
        try:
            cache_key = self._generate_cache_key(query, filters)

            if self.disabled:
                _IN_MEMORY_CACHE[cache_key] = results
                _IN_MEMORY_METRICS["cache_write"] = _IN_MEMORY_METRICS.get("cache_write", 0) + 1
                return True

            client = await self.get_client()
            await client.setex(
                f"search:{cache_key}",
                REDIS_TTL_CACHE,
                json.dumps(results)
            )
            await self.incr_metric("cache_write")
            return True
        except Exception as e:
            logger.error(f"Error setting search cache: {e}")
            return False

    # ========== Metrics ==========

    async def incr_metric(self, metric_name: str) -> int:
        """Increment metric counter"""
        try:
            if self.disabled:
                _IN_MEMORY_METRICS[metric_name] = _IN_MEMORY_METRICS.get(metric_name, 0) + 1
                return _IN_MEMORY_METRICS[metric_name]

            client = await self.get_client()
            count = await client.incr(f"metrics:{metric_name}")
            return count
        except Exception as e:
            logger.error(f"Error incrementing metric: {e}")
            return 0

    async def get_metric(self, metric_name: str) -> int:
        """Get metric value"""
        try:
            if self.disabled:
                return _IN_MEMORY_METRICS.get(metric_name, 0)

            client = await self.get_client()
            value = await client.get(f"metrics:{metric_name}")
            return int(value) if value else 0
        except Exception as e:
            logger.error(f"Error getting metric: {e}")
            return 0

    async def get_all_metrics(self) -> Dict[str, int]:
        """Get all metrics"""
        try:
            if self.disabled:
                return dict(_IN_MEMORY_METRICS)

            client = await self.get_client()
            keys = await client.keys("metrics:*")
            metrics = {}
            for key in keys:
                metric_name = key.replace("metrics:", "")
                value = await client.get(key)
                metrics[metric_name] = int(value) if value else 0
            return metrics
        except Exception as e:
            logger.error(f"Error getting all metrics: {e}")
            return {}

    async def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache hit/miss statistics"""
        hits = await self.get_metric("cache_hit")
        misses = await self.get_metric("cache_miss")
        total = hits + misses
        hit_rate = (hits / total * 100) if total > 0 else 0

        return {
            "cache_hit": hits,
            "cache_miss": misses,
            "total_requests": total,
            "hit_rate_percent": round(hit_rate, 2)
        }


# Global instance
redis_client = RedisClient()
