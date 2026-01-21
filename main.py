"""
PazarGlobal Agent API - Main Application
FastAPI application with WhatsApp and WebChat integration
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from config import settings
from api import whatsapp, webchat
from services.logger import get_logger
from services.monitoring import monitoring_router
from services.alerting import get_alerting_service

# Shared logger instance for this module
logger = get_logger(__name__)

# Create FastAPI app
app = FastAPI(
    title="PazarGlobal Agent API",
    description="AI Agent system for PazarGlobal marketplace with WhatsApp and WebChat support",
    version="2.0.0",
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
app.include_router(whatsapp.router)
app.include_router(webchat.router)
app.include_router(monitoring_router)


@app.post("/agent/run")
async def agent_run(request: Request):
    """
    Unified agent endpoint for Edge Function traffic
    Routes WhatsApp and WebChat requests to appropriate handlers
    
    Expected payload from Edge Function:
    {
        "user_id": str,
        "phone": str,
        "message": str,
        "conversation_history": List[dict],
        "media_paths": List[str] (optional),
        "media_type": str (optional),
        "draft_listing_id": str (optional),
        "session_token": str (optional),
        "user_context": dict (optional)
    }
    """
    try:
        data = await request.json()
        logger.info(f"🎯 /agent/run called - user_id: {data.get('user_id')}, message: {data.get('message')[:50]}")
        
        # Import process function from webchat
        from api.webchat import process_webchat_message
        
        # Convert Edge Function format to webchat format
        result = await process_webchat_message(
            message_body=data.get("message", ""),
            session_id=data.get("session_token") or data.get("user_id"),  # Use session_token or user_id as session
            user_id=data.get("user_id"),
            media_url=data.get("media_paths", [None])[0] if data.get("media_paths") else None,
            media_urls=data.get("media_paths")
        )
        
        # Return in format Edge Function expects
        return {
            "success": result.get("success", True),
            "response": result.get("message", ""),
            "intent": result.get("intent"),
            "data": result.get("data")
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
        "version": "2.0.0",
        "status": "active",
        "endpoints": {
            "whatsapp": "/whatsapp/webhook",
            "webchat": "/webchat/message",
            "websocket": "/webchat/ws/{session_id}",
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
