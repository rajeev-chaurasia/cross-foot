"""Runtime configuration, loaded from environment variables and .env."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-2.5-flash"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CROSSFOOT_", env_file=".env", extra="ignore")

    llm_base_url: str = GEMINI_OPENAI_BASE_URL
    llm_api_key: str = ""
    llm_model: str = DEFAULT_MODEL
    llm_timeout_seconds: float = 120.0
    data_dir: Path = Path("data")
