"""
Application settings and configuration
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
from pathlib import Path


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    model_config = SettingsConfigDict(
        # Always load the .env that belongs to this project, regardless of where
        # the process is launched from (repo root, Railway, etc.).
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
        case_sensitive=False,
        # Allow unrelated/extra env vars (e.g., deployment tokens) without crashing.
        extra="ignore",
    )
    
    # OpenAI Configuration
    openai_api_key: str
    openai_model: str = "gpt-4o"
    openai_vision_model: str = "gpt-4o-mini"
    openai_temperature: float = 0.7
    openai_max_tokens: int = 1500
    
    # Supabase Configuration
    supabase_url: str
    supabase_key: str
    supabase_service_key: str
    
    # Redis Configuration
    redis_url: str = "redis://localhost:6379"
    redis_db: int = 0
    redis_decode_responses: bool = True
    
    # WhatsApp/Twilio Configuration
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_whatsapp_number: Optional[str] = None
    
    # API Configuration
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_env: str = "development"
    
    # Webhook Configuration
    webhook_base_url: Optional[str] = None
    
    # Application Settings
    debug: bool = False
    log_level: str = "INFO"
    max_draft_age_hours: int = 24
    listing_credit_cost: int = 55
    
    # Rate Limiting
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    
# Global settings instance
settings = Settings()
