from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "AI Link Analysis API"
    app_version: str = "1.0.0"
    log_level: str = "INFO"

    # LLM
    llm_provider: str = "gemini"
    llm_model: str = "gemini-3-flash-preview"
    llm_fallback_provider: str | None = None
    llm_fallback_model: str | None = None
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 1
    llm_max_output_tokens: int = 12000
    llm_temperature: float = 0.3
    gemini_thinking_level: str | None = None
    openai_reasoning_effort: str | None = None

    gemini_api_key: str | None = None
    openai_api_key: str | None = None
    groq_api_key: str | None = None

    # Website fetching
    fetch_timeout_seconds: float = 12.0
    max_page_bytes: int = 2_500_000
    max_redirects: int = 5
    max_extra_pages: int = 3
    max_content_chars: int = 14_000
    verify_competitor_sites: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36 LinkAnalysisBot/1.0"
    )

    # Headless browser fallback for JavaScript-rendered sites (React, Vue, Angular...)
    enable_js_rendering: bool = True
    js_render_min_chars: int = 400  # render with a browser when static HTML has less text than this
    js_render_timeout_seconds: float = 20.0
    js_render_max_concurrent: int = 2
    js_render_extra_pages: int = 2

    # Cache
    cache_ttl_seconds: int = 3600
    cache_max_items: int = 200

    # Requests per minute per client IP on /link-analysis (0 = off)
    rate_limit_per_minute: int = 10

    @field_validator(
        "llm_fallback_provider",
        "llm_fallback_model",
        "gemini_thinking_level",
        "openai_reasoning_effort",
        "gemini_api_key",
        "openai_api_key",
        "groq_api_key",
        mode="before",
    )
    @classmethod
    def empty_to_none(cls, value):
        # Lets people leave "KEY=" lines empty in .env without breaking anything
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("llm_provider", "llm_fallback_provider", mode="after")
    @classmethod
    def lower_provider(cls, value):
        return value.lower().strip() if value else value


# USD per 1M tokens (input, output), standard paid tier.
# Taken from the official pricing pages in November 2026 - see README for sources.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.6-flash": (0.75, 3.75),  # promo price until 31 Dec 2026, then 1.50 / 7.50
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-4.1-mini": (0.40, 1.60),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = MODEL_PRICING.get(model)
    if not price:
        return None
    input_price, output_price = price
    return round((input_tokens * input_price + output_tokens * output_price) / 1_000_000, 6)


@lru_cache
def get_settings() -> Settings:
    return Settings()
