"""
PazarGlobal Agent API - Main Application
FastAPI application with WhatsApp and WebChat integration

v3.0.0 - Single LLM Brain architecture
Updated: 2026-02-02 17:00
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from config import settings
from routers.gateway_v3 import router as gateway_v3_router
from services.logger import get_logger
from services.monitoring import monitoring_router
from services.alerting import get_alerting_service

# Shared logger instance for this module
logger = get_logger(__name__)

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
    allow_origins=["*"],  # Configure based on your frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Include routers
app.include_router(gateway_v3_router)  # V3 - Single LLM Brain
app.include_router(monitoring_router)


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
        
        # Convert Edge Function format to V3 format
        v3_request = MessageRequest(
            user_id=data.get("user_id") or data.get("phone") or "unknown",
            message=data.get("message", ""),
            media_urls=data.get("media_paths"),
            channel="whatsapp"
        )
        
        result = await handle_message(v3_request)
        
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
            "agent_run": "/agent/run",
            "health": "/health",
            "docs": "/docs"
        }
    }


@app.get("/health")
async def health_check():
    """Health check endpoint for Railway"""
    return {
        "status": "healthy",
        "service": "pazarglobal-agent",
        "environment": settings.api_env
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
