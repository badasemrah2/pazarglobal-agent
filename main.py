"""
PazarGlobal Agent API - Main Application
FastAPI application with WhatsApp and WebChat integration
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from config import settings
from api import whatsapp, webchat
import sys

# Configure logger
logger.remove()
logger.add(
    sys.stderr,
    level=settings.log_level,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
)

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
