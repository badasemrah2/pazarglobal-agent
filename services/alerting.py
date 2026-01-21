"""
Internal alerting system - no external dependencies required.
Sends alerts via Telegram Bot API or logs to audit_logs table.
"""
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime
import httpx
from services.logger import logger
from services.supabase_client import get_supabase_client


class AlertingService:
    """Internal alerting without external service dependencies"""
    
    def __init__(self):
        self.telegram_bot_token: Optional[str] = None
        self.telegram_chat_id: Optional[str] = None
        self.alert_threshold = {
            "redis_latency_ms": 200,  # Alert if >200ms
            "orphan_drafts": 10,      # Alert if >10 orphans
            "draft_conflicts": 5       # Alert if >5 conflicts in 1h
        }
    
    def configure_telegram(self, bot_token: str, chat_id: str):
        """Optional: Configure Telegram for alerts (no signup required)"""
        self.telegram_bot_token = bot_token
        self.telegram_chat_id = chat_id
        logger.info(f"✅ Telegram alerting configured (chat_id: {chat_id})")
    
    async def send_alert(self, severity: str, message: str, details: Dict[str, Any] = None):
        """
        Send alert via multiple channels:
        1. Supabase audit_logs (always)
        2. Telegram (if configured)
        3. Logger (always)
        """
        timestamp = datetime.utcnow().isoformat()
        
        # 1. Log to Supabase audit_logs
        try:
            supabase = get_supabase_client()
            supabase.table("audit_logs").insert({
                "user_id": "system",
                "action": f"alert_{severity}",
                "metadata": {
                    "message": message,
                    "details": details or {},
                    "timestamp": timestamp
                }
            }).execute()
        except Exception as e:
            logger.error(f"Failed to log alert to Supabase: {e}")
        
        # 2. Send to Telegram (if configured)
        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                await self._send_telegram(severity, message, details)
            except Exception as e:
                logger.error(f"Failed to send Telegram alert: {e}")
        
        # 3. Log to application logs
        log_func = logger.critical if severity == "critical" else logger.warning
        log_func(f"🚨 ALERT [{severity.upper()}]: {message} | {details}")
    
    async def _send_telegram(self, severity: str, message: str, details: Dict[str, Any] = None):
        """Send formatted message to Telegram"""
        emoji = "🔴" if severity == "critical" else "⚠️"
        text = f"{emoji} **PazarGlobal Alert**\n\n"
        text += f"**Severity:** {severity.upper()}\n"
        text += f"**Message:** {message}\n"
        if details:
            text += f"\n**Details:**\n"
            for key, value in details.items():
                text += f"  • {key}: {value}\n"
        text += f"\n**Time:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        
        async with httpx.AsyncClient() as client:
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            await client.post(url, json={
                "chat_id": self.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }, timeout=5.0)
    
    async def check_and_alert(self, health_data: Dict[str, Any]):
        """
        Check health data and send alerts if thresholds exceeded.
        Called by monitoring endpoint.
        """
        checks = health_data.get("checks", {})
        
        # Check Redis latency
        redis_check = checks.get("redis", {})
        if redis_check.get("healthy") is False:
            await self.send_alert(
                severity="critical",
                message="Redis connection failed",
                details={"status": redis_check.get("status")}
            )
        elif redis_check.get("latency_ms", 0) > self.alert_threshold["redis_latency_ms"]:
            await self.send_alert(
                severity="warning",
                message=f"Redis latency high: {redis_check['latency_ms']}ms",
                details={"threshold": self.alert_threshold["redis_latency_ms"]}
            )
        
        # Check orphaned drafts
        orphans = checks.get("draft_orphans", {}).get("count", 0)
        if orphans > self.alert_threshold["orphan_drafts"]:
            await self.send_alert(
                severity="warning",
                message=f"High orphaned drafts: {orphans}",
                details={"threshold": self.alert_threshold["orphan_drafts"]}
            )
        
        # Check draft conflicts
        conflicts = checks.get("draft_conflicts", {}).get("count_24h", 0)
        if conflicts > self.alert_threshold["draft_conflicts"]:
            await self.send_alert(
                severity="warning",
                message=f"High draft conflicts: {conflicts} in 24h",
                details={"threshold": self.alert_threshold["draft_conflicts"]}
            )


# Singleton instance
_alerting_service = AlertingService()

def get_alerting_service() -> AlertingService:
    """Get singleton alerting service"""
    return _alerting_service
