"""
Redis atomic operations using Lua scripts
Prevents race conditions in session state management
"""
from typing import Dict, Any, Optional
import json
from loguru import logger


# Lua script for atomic read-modify-write session update
ATOMIC_UPDATE_SESSION_LUA = """
local session_key = KEYS[1]
local field = ARGV[1]
local value = ARGV[2]
local ttl = tonumber(ARGV[3])

-- Get existing session
local session_json = redis.call('GET', session_key)
local session = {}

if session_json then
    session = cjson.decode(session_json)
end

-- Update field
session[field] = cjson.decode(value)

-- Save back with TTL
local new_json = cjson.encode(session)
redis.call('SETEX', session_key, ttl, new_json)

return new_json
"""

# Lua script for atomic session merge (multiple fields)
ATOMIC_MERGE_SESSION_LUA = """
local session_key = KEYS[1]
local updates_json = ARGV[1]
local ttl = tonumber(ARGV[2])

-- Get existing session
local session_json = redis.call('GET', session_key)
local session = {}

if session_json then
    session = cjson.decode(session_json)
end

-- Merge updates
local updates = cjson.decode(updates_json)
for k, v in pairs(updates) do
    session[k] = v
end

-- Save back with TTL
local new_json = cjson.encode(session)
redis.call('SETEX', session_key, ttl, new_json)

return new_json
"""


class RedisAtomicOperations:
    """Wrapper for atomic Redis operations using Lua scripts"""
    
    def __init__(self, redis_client):
        """
        Args:
            redis_client: Instance of RedisClient from services/redis_client.py
        """
        self.redis_client = redis_client
        self._update_script_sha = None
        self._merge_script_sha = None
    
    async def _ensure_scripts_loaded(self):
        """Load Lua scripts into Redis (cache SHA for reuse)"""
        if self.redis_client.disabled:
            return  # Skip for in-memory fallback
        
        try:
            client = await self.redis_client.get_client()
            
            if not self._update_script_sha:
                self._update_script_sha = await client.script_load(ATOMIC_UPDATE_SESSION_LUA)
                logger.debug(f"Loaded atomic update script: {self._update_script_sha}")
            
            if not self._merge_script_sha:
                self._merge_script_sha = await client.script_load(ATOMIC_MERGE_SESSION_LUA)
                logger.debug(f"Loaded atomic merge script: {self._merge_script_sha}")
        except Exception as e:
            logger.error(f"Failed to load Lua scripts: {e}")
            raise
    
    async def atomic_update_field(
        self, 
        session_id: str, 
        field: str, 
        value: Any, 
        ttl: int = 86400
    ) -> bool:
        """
        Atomically update a single field in session state
        
        Args:
            session_id: Session identifier
            field: Field name to update
            value: New value (will be JSON serialized)
            ttl: Session TTL in seconds
        
        Returns:
            True if successful, False otherwise
        
        Example:
            await atomic.atomic_update_field("sess_123", "locked_intent", "create_listing")
        """
        # Fallback for in-memory mode
        if self.redis_client.disabled:
            from api.webchat_store import IN_MEMORY_SESSION_CACHE
            session = IN_MEMORY_SESSION_CACHE.get(session_id, {})
            session[field] = value
            IN_MEMORY_SESSION_CACHE[session_id] = session
            return True
        
        try:
            await self._ensure_scripts_loaded()
            client = await self.redis_client.get_client()
            
            session_key = f"session:{session_id}"
            value_json = json.dumps(value)
            
            result = await client.evalsha(
                self._update_script_sha,
                1,  # number of keys
                session_key,
                field,
                value_json,
                str(ttl)
            )
            
            logger.debug(f"Atomically updated {field} in session {session_id[:8]}...")
            return True
        except Exception as e:
            logger.error(f"Atomic field update failed: {e}")
            return False
    
    async def atomic_merge_updates(
        self,
        session_id: str,
        updates: Dict[str, Any],
        ttl: int = 86400
    ) -> bool:
        """
        Atomically merge multiple fields into session state
        
        Args:
            session_id: Session identifier
            updates: Dictionary of field:value pairs to merge
            ttl: Session TTL in seconds
        
        Returns:
            True if successful, False otherwise
        
        Example:
            await atomic.atomic_merge_updates("sess_123", {
                "locked_intent": "create_listing",
                "fsm_state": "active",
                "last_user_at": "2026-01-22T10:00:00Z"
            })
        """
        # Fallback for in-memory mode
        if self.redis_client.disabled:
            from api.webchat_store import IN_MEMORY_SESSION_CACHE
            session = IN_MEMORY_SESSION_CACHE.get(session_id, {})
            session.update(updates)
            IN_MEMORY_SESSION_CACHE[session_id] = session
            return True
        
        try:
            await self._ensure_scripts_loaded()
            client = await self.redis_client.get_client()
            
            session_key = f"session:{session_id}"
            updates_json = json.dumps(updates)
            
            result = await client.evalsha(
                self._merge_script_sha,
                1,  # number of keys
                session_key,
                updates_json,
                str(ttl)
            )
            
            logger.debug(f"Atomically merged {len(updates)} fields in session {session_id[:8]}...")
            return True
        except Exception as e:
            logger.error(f"Atomic merge failed: {e}")
            return False
    
    async def get_session_with_lock(
        self,
        session_id: str,
        lock_timeout: int = 5
    ) -> Optional[Dict[str, Any]]:
        """
        Get session with distributed lock (for critical sections)
        
        WARNING: Caller MUST release lock via release_session_lock()
        
        Args:
            session_id: Session identifier
            lock_timeout: Lock expiry in seconds
        
        Returns:
            Session dict if lock acquired, None otherwise
        """
        # Fallback: No locking in memory mode (single-threaded assumption)
        if self.redis_client.disabled:
            from api.webchat_store import IN_MEMORY_SESSION_CACHE
            return IN_MEMORY_SESSION_CACHE.get(session_id)
        
        try:
            client = await self.redis_client.get_client()
            lock_key = f"lock:session:{session_id}"
            
            # Try to acquire lock (SET NX EX)
            locked = await client.set(lock_key, "1", nx=True, ex=lock_timeout)
            
            if not locked:
                logger.warning(f"Failed to acquire lock for session {session_id[:8]}...")
                return None
            
            # Get session data
            session = await self.redis_client.get_session(session_id)
            return session
        except Exception as e:
            logger.error(f"Lock acquisition failed: {e}")
            return None
    
    async def release_session_lock(self, session_id: str) -> bool:
        """Release distributed lock for session"""
        if self.redis_client.disabled:
            return True  # No-op in memory mode
        
        try:
            client = await self.redis_client.get_client()
            lock_key = f"lock:session:{session_id}"
            await client.delete(lock_key)
            logger.debug(f"Released lock for session {session_id[:8]}...")
            return True
        except Exception as e:
            logger.error(f"Lock release failed: {e}")
            return False


# Singleton instance
_atomic_ops = None


def get_atomic_ops(redis_client) -> RedisAtomicOperations:
    """Get singleton instance of atomic operations wrapper"""
    global _atomic_ops
    if _atomic_ops is None:
        _atomic_ops = RedisAtomicOperations(redis_client)
    return _atomic_ops
