from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ollama_base_url: str = "http://XXX.XXX.XX.XX:00000"
    ollama_model: str = "XXXXXX:00X"
    ollama_timeout_seconds: int = 00

    fallback_provider: str = "XXX"
    fallback_api_key: str = "XXXXXX-XXXX000-XXX00-XXX"
    fallback_model: str = "XXXX-0-XXXXXX"

    twilio_account_sid: str = "XX00000000000000000000000000000000"
    twilio_auth_token: str = "XX00000000000000000000000000000000"
    twilio_phone_number: str = "+0-000-000-0000"

    stt_provider: str = "XXXXXXX-XXXXX"
    stt_api_key: str = "XXXXXX-XXXX000-XXX00-XXX"

    tts_provider: str = "XXXXXXXX"
    tts_api_key: str = "XXXXXX-XXXX000-XXX00-XXX"
    tts_voice_id: str = "XXXXXX-XXXX000-XXX00-XXX"

    qbo_client_id: str = "XX00000000000000000000000000000000"
    qbo_client_secret: str = "XX00000000000000000000000000000000"
    qbo_refresh_token: str = "XX00000000000000000000000000000000"
    qbo_access_token: str = "XX00000000000000000000000000000000"
    qbo_realm_id: str = "0000000000000000000"
    qbo_environment: str = "sandbox"  # "sandbox" | "production"

    smtp_host: str = "XXXX.XXXXX.XXX"
    smtp_port: int = 000
    smtp_user: str = "XXXXX@XXXXX.XXX"
    smtp_password: str = "XXXXXX-XXXX000-XXX00-XXX"
    smtp_from_address: str = "XXXXX@XXXXX.XXX"  # defaults to smtp_user if empty
    smtp_from_name: str = "XXX & XXXXXXXX XXXXXX"
    email_demo_redirect: str = ""  # if set, all outbound reroutes here

    telegram_bot_token: str = "0000000000:XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
    telegram_allowed_chat_ids: str = ""  # comma-separated list of chat IDs

    database_url: str = "sqlite:///./data/orchestrator.db"
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    @property
    def data_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "data"


settings = Settings()
