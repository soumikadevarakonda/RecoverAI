from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env, resolved from this file so it loads no matter which directory
# uvicorn / alembic / pytest is started from. A .env in the current working
# directory (if any) is read afterwards and takes precedence.
BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    database_url: str

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    dev_merchant_id: str = ""
    environment: str = "development"
    auth_mode: str = "development"

    # Secret used to sign merchant API tokens in production auth mode.
    # Generate a token with: python backend/scripts/issue_merchant_token.py <merchant_uuid>
    auth_token_secret: str = ""

    # Comma-separated list of browser origins allowed to call the API.
    cors_allowed_origins: str = (
        "http://127.0.0.1:5173,http://localhost:5173,"
        "http://127.0.0.1:5174,http://localhost:5174"
    )

    # Optional LLM strategist. When empty, recovery decisions use the
    # deterministic economics engine only.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_production(self) -> bool:
        return self.environment == "production" or self.auth_mode == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]


settings = Settings()
