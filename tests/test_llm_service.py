import json

import httpx
import pytest

from app.config import Settings
from app.exceptions import LLMError, LLMNotConfiguredError
from app.services import llm_service
from app.services.llm_service import GeminiClient, GroqClient, OpenAICompatibleClient, build_client


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(llm_service.asyncio, "sleep", instant)


def make_settings(**overrides):
    values = dict(_env_file=None, gemini_api_key="g-key", openai_api_key="o-key", groq_api_key="q-key")
    values.update(overrides)
    return Settings(**values)


def gemini_body(text, finish="STOP"):
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": "thinking...", "thought": True}, {"text": text}]},
                "finishReason": finish,
            }
        ],
        "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500, "thoughtsTokenCount": 200},
    }


async def test_gemini_request_and_response():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=gemini_body('{"ok": true}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GeminiClient(http, "gemini-3.5-flash-lite", make_settings(gemini_thinking_level="low"))
        result = await client.generate_json("system", "user")

    assert seen["url"].endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert seen["key"] == "g-key"
    config = seen["body"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["thinkingConfig"] == {"thinkingLevel": "low"}
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "system"
    assert result.data == {"ok": True}
    assert result.input_tokens == 1000
    assert result.output_tokens == 700  # thinking tokens are billed as output


async def test_gemini_retries_without_thinking_when_unsupported():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if "thinkingConfig" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Thinking level is not supported for this model."}})
        return httpx.Response(200, json=gemini_body('{"ok": 1}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GeminiClient(http, "gemini-3.1-flash-lite", make_settings(gemini_thinking_level="low"))
        result = await client.generate_json("s", "u")

    assert len(calls) == 2
    assert result.data == {"ok": 1}


async def test_gemini_truncated_output_raises():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=gemini_body('{"a":', finish="MAX_TOKENS")))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(LLMError) as err:
            await GeminiClient(http, "gemini-3.5-flash-lite", make_settings()).generate_json("s", "u")
    assert err.value.code == "llm_truncated"


async def test_rate_limit_is_retried_once():
    responses = iter([httpx.Response(429, json={"error": {"message": "slow down"}}), httpx.Response(200, json=gemini_body("{}"))])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(responses))) as http:
        result = await GeminiClient(http, "gemini-3.5-flash-lite", make_settings()).generate_json("s", "u")
    assert result.data == {}


async def test_bad_api_key_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": "invalid key"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LLMError) as err:
            await GeminiClient(http, "gemini-3.5-flash-lite", make_settings()).generate_json("s", "u")
    assert err.value.code == "llm_auth_failed"
    assert len(calls) == 1


async def test_missing_key_raises_not_configured():
    async with httpx.AsyncClient() as http:
        with pytest.raises(LLMNotConfiguredError):
            await GeminiClient(http, "gemini-3.5-flash-lite", make_settings(gemini_api_key=None)).generate_json("s", "u")


async def test_openai_payload_for_gpt5_models():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"x": 1}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 400},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(http, "gpt-5-mini", make_settings(openai_reasoning_effort="low"))
        result = await client.generate_json("s", "u")

    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer o-key"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["reasoning_effort"] == "low"
    assert "temperature" not in seen["body"]
    assert result.data == {"x": 1}
    assert (result.input_tokens, result.output_tokens) == (900, 400)


async def test_groq_uses_its_own_endpoint_and_key():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = build_client("groq", "openai/gpt-oss-120b", http, make_settings(openai_reasoning_effort="minimal"))
        assert isinstance(client, GroqClient)
        await client.generate_json("s", "u")

    assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert seen["auth"] == "Bearer q-key"
    assert seen["body"]["reasoning_effort"] == "low"
    assert "temperature" in seen["body"]


def test_unknown_provider():
    with pytest.raises(LLMNotConfiguredError):
        build_client("nope", "model", None, make_settings())
