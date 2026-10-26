import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    base_url: str = os.environ.get("RENDER_EXTERNAL_URL") or "https://business-padi.onrender.com"
    database_url: str = "sqlite:///./ledger.db"
    tz: str = "Africa/Lagos"

    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = "business_padi_webhook_secret_2026"
    whatsapp_app_secret: str = ""
    graph_api_version: str = "v20.0"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = "whatsapp:+14155238886"

    asr_provider: str = os.environ.get("ASR_PROVIDER", "groq")
    asr_model: str = os.environ.get("ASR_MODEL", "whisper-large-v3")
    asr_api_key: str = os.environ.get("ASR_API_KEY", "")
    llm_provider: str = os.environ.get("LLM_PROVIDER", "groq")
    llm_model: str = os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b")
    llm_api_key: str = os.environ.get("LLM_API_KEY", "")
    tts_provider: str = "yarngpt"
    yarngpt_api_key: str = "sk_live_10ypR9T5hMJkcyCK.auXXCXsSJjMSN7RUjsqjTSHULCFs56OtrEZVgegBOwQ"
    telegram_bot_token: str = "8385879089:AAGbfSdub46z9c7JwfeMtLhUC6I7zEzJ-Ik"

    languages_enabled: str = "pcm,en,yo,ha,ig"
    asr_provider_by_lang: str = ""
    max_voice_seconds: int = 90
    default_language: str = "pcm"
    admin_chat_ids: str = ""

    usd_ngn: float = 1400
    wa_usd_per_message: float = 0.0077
    ai_cost_per_entry_kobo: int = 1500
    daily_message_cost_alert_kobo: int | None = None
    demo_mode: bool = False
    rate_limit_per_minute: int = 30
    log_format: str = "json"


settings = Settings()
