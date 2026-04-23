from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ollama_base_url: str = "http://192.168.10.33:11434"
    ollama_model: str = "gemma4:31b"
    ollama_timeout_seconds: int = 60

    fallback_provider: str = "xai"
    fallback_api_key: str = ""
    fallback_model: str = "grok-3-latest"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    stt_provider: str = "whisper-local"
    stt_api_key: str = ""

    tts_provider: str = "cartesia"
    tts_api_key: str = ""
    tts_voice_id: str = ""

    qbo_client_id: str = ""
    qbo_client_secret: str = ""
    qbo_refresh_token: str = ""
    qbo_access_token: str = ""
    qbo_realm_id: str = ""
    qbo_environment: str = "sandbox"  # "sandbox" | "production"

    database_url: str = "sqlite:///./data/orchestrator.db"
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"

    @property
    def data_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent / "data"


settings = Settings()
