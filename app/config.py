"""Application settings, loaded from environment variables and the project's .env file."""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """All tunable values. Defaults are sized for the Groq free tier; override any of them in .env."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM provider
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: Literal["low", "medium", "high"] = "low"
    llm_call_timeout_s: float = Field(30.0, gt=0)
    llm_schema_retries: int = Field(2, ge=0)
    llm_max_rate_limit_waits: int = Field(3, ge=0)
    llm_rate_limit_fallback_wait_s: float = Field(10.0, gt=0)

    # Token budget
    request_token_ceiling: int = Field(40_000, gt=0)  # a maximum, not expected usage
    per_call_token_cap: int = Field(6_500, gt=0)
    pacer_window_tokens: int = Field(7_500, gt=0)
    pacer_window_s: float = Field(60.0, gt=0)
    completion_tokens_task: int = Field(2_000, gt=0)
    completion_tokens_analyze: int = Field(2_500, gt=0)

    # Token estimator
    estimate_chars_per_token: float = Field(3.0, gt=0)
    estimate_tokens_per_message: int = Field(10, ge=0)
    estimate_margin: float = Field(0.2, ge=0)

    # Request limits
    request_deadline_s: float = Field(480.0, gt=0)
    max_request_chars: int = Field(6_000, gt=0)
    min_plan_tasks: int = Field(2, ge=1)
    max_plan_tasks: int = Field(6, ge=1)
    max_repair_sections: int = Field(3, ge=0)

    # Paths and logging
    output_dir: Path = PROJECT_ROOT / "outputs"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_budgets_are_consistent(self) -> "Settings":
        """Reject combinations that could never produce a valid call or plan."""
        if self.per_call_token_cap > self.pacer_window_tokens:
            raise ValueError("PER_CALL_TOKEN_CAP must not exceed PACER_WINDOW_TOKENS")
        if max(self.completion_tokens_task, self.completion_tokens_analyze) >= self.per_call_token_cap:
            raise ValueError("Completion token caps must be below PER_CALL_TOKEN_CAP")
        if self.min_plan_tasks > self.max_plan_tasks:
            raise ValueError("MIN_PLAN_TASKS must not exceed MAX_PLAN_TASKS")
        return self

    @field_validator("output_dir")
    @classmethod
    def _resolve_output_dir(cls, value: Path) -> Path:
        """Interpret a relative OUTPUT_DIR from the project root, not the working directory."""
        return value if value.is_absolute() else PROJECT_ROOT / value


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()


def configure_logging(level: str) -> None:
    """Configure one consistent log format for the whole application."""
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
