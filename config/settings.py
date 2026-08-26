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
    
    # Perplexity API Configuration
    perplexity_api_key: Optional[str] = None
    
    # Shared secret between the Supabase Edge traffic controller and this backend.
    # When set, any request claiming channel="whatsapp" must present it. Left unset the
    # check only warns, so the backend can be deployed before the secret is provisioned.
    internal_api_secret: Optional[str] = None

    # API Configuration
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_env: str = "development"
    cors_allowed_origins: str = "http://localhost:5173,http://localhost:5174"
    
    # Webhook Configuration
    webhook_base_url: Optional[str] = None
    frontend_base_url: Optional[str] = "https://pazarglobal.com"
    
    # Application Settings
    debug: bool = False
    log_level: str = "INFO"
    max_draft_age_hours: int = 24
    listing_credit_cost: int = 55

    # Feature flags
    enable_metadata_keyword_search: bool = True
    # Hide listings whose expires_at has passed from search results.
    #
    # This stayed off while sellers had no way to see or extend their deadline - enabling
    # it then would have removed most of the catalogue from people who were never warned.
    # The countdown and the "Yeniden Yayınla" button are live now, so an expired listing
    # is something its owner can bring back in one click, and search should stop showing
    # listings whose window has closed.
    #
    # "İlanlarım" is not affected: it queries listings directly and deliberately keeps
    # showing expired ones, which is where renewal happens.
    hide_expired_listings: bool = True
    
    # Rate Limiting
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    
    # Alerting (Optional - for internal alerts without external services)
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    
# Global settings instance
settings = Settings()
