"""
PazarGlobal Agent API - Main Application
FastAPI application with WhatsApp and WebChat integration

v3.0.0 - Single LLM Brain architecture
Updated: 2026-02-02 17:00
"""
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from config import settings
from routers.gateway_v3 import router as gateway_v3_router
from routers.admin import router as admin_router
from routers.contact import router as contact_router
from services.logger import get_logger
from services.monitoring import monitoring_router
from services.alerting import get_alerting_service
from services.supabase_client import supabase_client

# Shared logger instance for this module
logger = get_logger(__name__)

allowed_origins = [
    origin.strip()
    for origin in (settings.cors_allowed_origins or "").split(",")
    if origin.strip()
]

# Create FastAPI app
app = FastAPI(
    title="PazarGlobal Agent API",
    description="AI Agent system for PazarGlobal marketplace with WhatsApp and WebChat support",
    version="3.0.0",
    debug=settings.debug
)

# CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include routers
app.include_router(gateway_v3_router)  # V3 - Single LLM Brain
app.include_router(monitoring_router)
app.include_router(admin_router)
app.include_router(contact_router)


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


@app.get("/seo/sitemap-live.xml")
async def seo_sitemap_live() -> Response:
    """Live sitemap endpoint: returns listing URLs from DB without requiring redeploy."""
    site_url = "https://pazarglobal.com"
    now = datetime.now(timezone.utc)

    static_urls = [
        (f"{site_url}/", "daily", "1.0", None),
        (f"{site_url}/listings", "hourly", "0.9", None),
        (f"{site_url}/create-listing", "weekly", "0.7", None),
        (f"{site_url}/about", "monthly", "0.6", None),
        (f"{site_url}/reviews", "weekly", "0.7", None),
    ]

    listing_rows = []
    try:
        # 5000 cap keeps endpoint predictable while covering common marketplace scale.
        result = (
            supabase_client.client
            .table("listings")
            .select("id,status,expires_at,created_at,updated_at")
            .eq("status", "active")
            .order("created_at", desc=True)
            .limit(5000)
            .execute()
        )
        listing_rows = result.data if isinstance(result.data, list) else []
    except Exception as e:
        logger.error(f"Live sitemap query error: {e}")
        listing_rows = []

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]

    for loc, changefreq, priority, lastmod in static_urls:
        lines.append("  <url>")
        lines.append(f"    <loc>{_xml_escape(loc)}</loc>")
        if lastmod:
            lines.append(f"    <lastmod>{_xml_escape(lastmod)}</lastmod>")
        lines.append(f"    <changefreq>{_xml_escape(changefreq)}</changefreq>")
        lines.append(f"    <priority>{_xml_escape(priority)}</priority>")
        lines.append("  </url>")

    for row in listing_rows:
        listing_id = str((row or {}).get("id") or "").strip()
        if not listing_id:
            continue

        expires_at = (row or {}).get("expires_at")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if exp_dt < now:
                    continue
            except Exception:
                pass

        lastmod_raw = (row or {}).get("updated_at") or (row or {}).get("created_at")
        lastmod = None
        if lastmod_raw:
            try:
                lm = datetime.fromisoformat(str(lastmod_raw).replace("Z", "+00:00"))
                if lm.tzinfo is None:
                    lm = lm.replace(tzinfo=timezone.utc)
                lastmod = lm.isoformat()
            except Exception:
                lastmod = None

        lines.append("  <url>")
        lines.append(f"    <loc>{_xml_escape(f'{site_url}/listing/{listing_id}')}</loc>")
        if lastmod:
            lines.append(f"    <lastmod>{_xml_escape(lastmod)}</lastmod>")
        lines.append("    <changefreq>daily</changefreq>")
        lines.append("    <priority>0.8</priority>")
        lines.append("  </url>")

    lines.append("</urlset>")
    xml = "\n".join(lines) + "\n"

    return Response(
        content=xml,
        media_type="application/xml",
        headers={
            # Keep cache short so new listings appear quickly without aggressive origin load.
            "Cache-Control": "public, max-age=120",
        },
    )


@app.post("/agent/run")
async def agent_run(request: Request):
    """
    Unified agent endpoint for Edge Function traffic (WhatsApp bridge)
    Now routes to V3 gateway
    """
    try:
        data = await request.json()
        logger.info(f"🎯 /agent/run called - user_id: {data.get('user_id')}, message: {data.get('message', '')[:50]}")
        
        # Import V3 handler
        from routers.gateway_v3 import handle_message, MessageRequest
        
        # Convert Edge Function format to V3 format.
        # Channel comes from the payload (this endpoint is the WhatsApp bridge path, so
        # whatsapp stays the default) instead of being hardcoded, so a webchat caller
        # routed through here is not silently granted the JWT-free whatsapp trust path.
        raw_channel = str(data.get("channel") or data.get("source") or "whatsapp").strip().lower()
        channel = raw_channel if raw_channel in ("whatsapp", "webchat") else "whatsapp"

        v3_request = MessageRequest(
            user_id=data.get("user_id") or data.get("phone") or "unknown",
            message=data.get("message", ""),
            media_urls=data.get("media_paths"),
            channel=channel,
            prefill_listing_data=data.get("prefill_listing_data") if isinstance(data.get("prefill_listing_data"), dict) else None,
        )

        result = await handle_message(
            v3_request,
            authorization=request.headers.get("Authorization"),
            internal_secret=request.headers.get("X-Internal-Secret"),
        )
        
        # Return in format Edge Function expects
        return {
            "success": result.success,
            "response": result.text,
            "intent": result.metadata.get("intent") if result.metadata else None,
            "data": result.listing_preview
        }
        
    except Exception as e:
        logger.error(f"❌ /agent/run error: {e}", exc_info=True)
        return {
            "success": False,
            "response": "Üzgünüm, bir hata oluştu. Lütfen tekrar deneyin."
        }


@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "PazarGlobal Agent API",
        "version": "3.0.0",
        "status": "active",
        "endpoints": {
            "v3_message": "/api/v3/message",
            "webchat_media_analyze": "/webchat/media/analyze",
            "agent_run": "/agent/run",
            "health": "/health",
            "docs": "/docs"
        }
    }


@app.post("/webchat/media/analyze")
async def webchat_media_analyze(request: Request):
    """
    Legacy webchat media analyze endpoint.
    Routes to V3 gateway media analyze.
    """
    try:
        data = await request.json()
        logger.info(f"📷 /webchat/media/analyze - user_id: {data.get('user_id')}, urls: {len(data.get('media_urls', []))}")
        
        from routers.gateway_v3 import analyze_media, MediaAnalyzeRequest
        
        v3_request = MediaAnalyzeRequest(
            session_id=data.get("session_id", data.get("user_id", "")),
            user_id=data.get("user_id", ""),
            phone_number=data.get("phone_number"),
            media_urls=data.get("media_urls", [])
        )
        
        result = await analyze_media(v3_request)
        
        return {
            "success": result.success,
            "message": result.message,
            "response": result.message,  # Alias for frontend compatibility
            "data": result.data
        }
        
    except Exception as e:
        logger.error(f"❌ /webchat/media/analyze error: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"Görsel analiz hatası: {str(e)}",
            "response": f"Görsel analiz hatası: {str(e)}"
        }


@app.get("/health")
async def health_check():
    """Health check endpoint for Railway"""
    from config.settings import internal_secret_status

    # Reported here because it cannot be observed anywhere else: an unset whatsapp trust
    # secret fails open, so requests succeed and the logs say "Auth verified" either way.
    # This is the only way to check it without reading Railway's variables by hand.
    # It names variables and states, never the secret itself.
    return {
        "status": "healthy",
        "service": "pazarglobal-agent",
        "environment": settings.api_env,
        "whatsapp_trust_secret": internal_secret_status(),
    }


@app.post("/admin/clear-search-cache")
async def clear_search_cache():
    """Admin endpoint to clear search cache after metadata updates"""
    from services.redis_client import redis_client
    count = await redis_client.clear_search_cache()
    return {
        "status": "success",
        "message": f"Cleared {count} search cache entries",
        "cleared_count": count
    }


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler"""
    logger.error(f"Global error: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "message": str(exc) if settings.debug else "An error occurred"
        }
    )


@app.on_event("startup")
async def startup_event():
    """Startup event"""
    logger.info("🚀 PazarGlobal Agent API starting...")
    logger.info(f"Environment: {settings.api_env}")
    logger.info(f"Debug mode: {settings.debug}")
    
    # Configure alerting if credentials provided
    if settings.telegram_bot_token and settings.telegram_chat_id:
        alerting = get_alerting_service()
        alerting.configure_telegram(settings.telegram_bot_token, settings.telegram_chat_id)
        logger.info("✅ Telegram alerting enabled")
    
    logger.info("✅ API ready")


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown event"""
    logger.info("👋 PazarGlobal Agent API shutting down...")
    
    # Close Redis connection
    from services import redis_client
    await redis_client.close()
    
    logger.info("✅ Cleanup complete")


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug
    )
