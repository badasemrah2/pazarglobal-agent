"""
Application settings and configuration
"""
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
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
    listing_credit_cost: int = 1
    
    # Rate Limiting
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    
    class Config:
        env_file = ".env"
        case_sensitive = False


# Global settings instance
settings = Settings()
