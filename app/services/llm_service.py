"""Thin LLM clients that talk to the provider REST APIs directly with httpx.

Using the HTTP APIs instead of three different SDKs keeps the dependency list
short and lets every provider share the same timeout / retry handling.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.exceptions import LLMError, LLMNotConfiguredError

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass
class LLMResult:
    data: dict
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class BaseLLMClient:
    provider = "base"

    def __init__(self, http: httpx.AsyncClient, model: str, settings: Settings):
        self.http = http
        self.model = model
        self.settings = settings

    async def generate_json(self, system_prompt: str, user_prompt: str) -> LLMResult:
        raise NotImplementedError

    async def _post(self, url: str, payload: dict, headers: dict) -> dict:
        """POST with retries + backoff on rate limits / overload / transient errors."""
        last_error = None
        attempts = self.settings.llm_max_retries + 1
        for attempt in range(attempts):
            retry_after = None
            try:
                response = await self.http.post(
                    url, json=payload, headers=headers, timeout=self.settings.llm_timeout_seconds
                )
            except httpx.TimeoutException:
                last_error = LLMError(f"{self.provider} request timed out.", code="llm_timeout", status_code=504)
            except httpx.HTTPError as exc:
                last_error = LLMError(f"Could not reach {self.provider}: {exc.__class__.__name__}.")
            else:
                if response.status_code < 400:
                    return response.json()
                message = _error_message(response)
                if response.status_code in (401, 403):
                    raise LLMError(f"{self.provider} rejected the API key: {message}", code="llm_auth_failed")
                last_error = LLMError(f"{self.provider} returned HTTP {response.status_code}: {message}")
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
                retry_after = _retry_after_seconds(response)
            if attempt < attempts - 1:
                # 2s, 4s, ... unless the provider tells us how long to wait (capped)
                delay = min(retry_after or 2 * (attempt + 1), 10)
                logger.warning("%s/%s attempt %d failed (%s) - retrying in %ss", self.provider, self.model, attempt + 1, last_error.message[:120], delay)
                await asyncio.sleep(delay)
        raise last_error


class GeminiClient(BaseLLMClient):
    provider = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    async def generate_json(self, system_prompt: str, user_prompt: str) -> LLMResult:
        if not self.settings.gemini_api_key:
            raise LLMNotConfiguredError("GEMINI_API_KEY is not set.")

        generation_config = {
            "responseMimeType": "application/json",
            "temperature": self.settings.llm_temperature,
            "maxOutputTokens": self.settings.llm_max_output_tokens,
        }
        if self.settings.gemini_thinking_level and not self.model.startswith("gemini-2"):
            generation_config["thinkingConfig"] = {"thinkingLevel": self.settings.gemini_thinking_level}

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": generation_config,
        }
        headers = {"x-goog-api-key": self.settings.gemini_api_key}
        url = f"{self.base_url}/{self.model}:generateContent"

        started = time.perf_counter()
        try:
            body = await self._post(url, payload, headers)
        except LLMError as exc:
            # Older / smaller models may not accept a thinking level - retry once without it
            if "thinking" in exc.message.lower() and "thinkingConfig" in generation_config:
                generation_config.pop("thinkingConfig")
                body = await self._post(url, payload, headers)
            else:
                raise
        latency = int((time.perf_counter() - started) * 1000)

        feedback = body.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise LLMError(f"Gemini blocked the request: {feedback['blockReason']}")

        candidates = body.get("candidates") or []
        if not candidates:
            raise LLMError("Gemini returned no candidates.")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise LLMError("Gemini response was cut off (max output tokens reached).", code="llm_truncated")

        usage = body.get("usageMetadata") or {}
        output_tokens = (usage.get("candidatesTokenCount") or 0) + (usage.get("thoughtsTokenCount") or 0)
        return LLMResult(
            data=parse_json(text),
            provider=self.provider,
            model=self.model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=output_tokens or None,
            latency_ms=latency,
        )


class OpenAICompatibleClient(BaseLLMClient):
    """OpenAI Chat Completions API. Groq exposes the same API, so it reuses this class."""

    provider = "openai"
    base_url = "https://api.openai.com/v1"

    def _api_key(self) -> str | None:
        return self.settings.openai_api_key

    def _is_reasoning_model(self) -> bool:
        return self.model.startswith(("gpt-5", "o3", "o4")) or "gpt-oss" in self.model

    async def generate_json(self, system_prompt: str, user_prompt: str) -> LLMResult:
        api_key = self._api_key()
        if not api_key:
            raise LLMNotConfiguredError(f"API key for {self.provider} is not set.")

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": self.settings.llm_max_output_tokens,
        }
        effort = self.settings.openai_reasoning_effort
        if self._is_reasoning_model() and effort:
            if "gpt-oss" in self.model and effort == "minimal":
                effort = "low"  # gpt-oss only knows low / medium / high
            payload["reasoning_effort"] = effort
        # GPT-5 / o-series only accept the default temperature
        if not self.model.startswith(("gpt-5", "o3", "o4")):
            payload["temperature"] = self.settings.llm_temperature

        started = time.perf_counter()
        body = await self._post(
            f"{self.base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {api_key}"},
        )
        latency = int((time.perf_counter() - started) * 1000)

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"{self.provider} returned no choices.")
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise LLMError(f"{self.provider} response was cut off (max tokens reached).", code="llm_truncated")
        text = (choice.get("message") or {}).get("content") or ""

        usage = body.get("usage") or {}
        return LLMResult(
            data=parse_json(text),
            provider=self.provider,
            model=self.model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            latency_ms=latency,
        )


class GroqClient(OpenAICompatibleClient):
    provider = "groq"
    base_url = "https://api.groq.com/openai/v1"

    def _api_key(self) -> str | None:
        return self.settings.groq_api_key


PROVIDERS: dict[str, type[BaseLLMClient]] = {
    "gemini": GeminiClient,
    "openai": OpenAICompatibleClient,
    "groq": GroqClient,
}


def build_client(provider: str, model: str, http: httpx.AsyncClient, settings: Settings) -> BaseLLMClient:
    client_cls = PROVIDERS.get(provider)
    if client_cls is None:
        raise LLMNotConfiguredError(f"Unknown LLM provider '{provider}'. Use one of: {', '.join(PROVIDERS)}.")
    return client_cls(http, model, settings)


def parse_json(text: str) -> dict:
    """Parse model output as JSON, tolerating code fences or stray text around it."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("The model did not return valid JSON.", code="llm_invalid_json") from None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            raise LLMError("The model did not return valid JSON.", code="llm_invalid_json") from None
    # Some models wrap the object in a one-item array
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise LLMError("The model returned JSON but not an object.", code="llm_invalid_json")
    return data


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message", ""))[:300]
    return str(body)[:300]
