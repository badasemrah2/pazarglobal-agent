"""
Application settings and configuration
"""
from pydantic import AliasChoices, Field
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
    #
    # Two names are accepted because the two sides were named differently: the Edge
    # function reads BACKEND_INTERNAL_SECRET from Supabase, this backend read only
    # INTERNAL_API_SECRET from Railway. Setting the Edge function's name on Railway did
    # nothing - the value was never read - and because an unset secret fails open, that
    # produced no error, just a silently unauthenticated trust path.
    #
    # INTERNAL_API_SECRET wins when both are set; see _check_internal_secret_config(),
    # which reports it when the two disagree.
    internal_api_secret: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("INTERNAL_API_SECRET", "BACKEND_INTERNAL_SECRET"),
    )

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


def internal_secret_status() -> dict:
    """Describe how the whatsapp trust secret is configured, for /health.

    An unset secret makes verify_internal_secret() return True for everything, so a
    misconfiguration looks exactly like success: the Edge function gets 200 and the log
    still says "Auth verified". Nothing in the request path can tell the two apart, which
    is why this is surfaced separately instead of being left to a warning line.
    """
    import os

    # The state is derived from the value actually in use, not from the environment:
    # settings also loads the .env file, so a secret can be active while neither
    # environment variable is set. Reporting "not configured" in that case would be its
    # own piece of misleading health output.
    active = (settings.internal_api_secret or "").strip()
    primary = (os.getenv("INTERNAL_API_SECRET") or "").strip()
    fallback = (os.getenv("BACKEND_INTERNAL_SECRET") or "").strip()

    names = [n for n, v in (("INTERNAL_API_SECRET", primary),
                            ("BACKEND_INTERNAL_SECRET", fallback)) if v]

    if not active:
        state, using = "unauthenticated", None
    elif primary and fallback and primary != fallback:
        # Both names are set to different values and only one of them can be in use.
        # Whoever set the other one meant it to take effect, so this is a real
        # misconfiguration even while requests are still succeeding.
        state, using = "conflict", "INTERNAL_API_SECRET"
    else:
        state = "configured"
        if primary and primary == active:
            using = "INTERNAL_API_SECRET"
        elif fallback and fallback == active:
            using = "BACKEND_INTERNAL_SECRET"
        else:
            using = ".env file"

    return {"state": state, "configured_names": names, "using": using}
