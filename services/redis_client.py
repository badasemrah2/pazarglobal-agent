"""
Redis client for state management

TEMP DISABLED: No Redis instance in the current environment.
All methods short-circuit to safe defaults to keep the app running.
"""
from typing import Optional, Dict, Any
import json
from loguru import logger
# redis is intentionally not imported to avoid connection attempts when disabled


class RedisClient:
    """Redis client for session state management"""
    
    def __init__(self):
        self._client: Optional[Any] = None  # type: ignore
        self.disabled = True  # temporary: no Redis available
    
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
                return None
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
    
    async def update_session(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Update session state"""
        try:
            if self.disabled:
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
                return []
            client = await self.get_client()
            messages = await client.lrange(f"messages:{session_id}", 0, limit - 1)
            return [json.loads(msg) for msg in messages]
        except Exception as e:
            logger.error(f"Error getting messages: {e}")
            return []


# Global instance
redis_client = RedisClient()
