import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.exceptions import AppError, RateLimitError
from app.schemas import ErrorDetail, ErrorResponse, LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import AnalysisService
from app.services.competitor_service import CompetitorService
from app.services.js_renderer import JSRenderer
from app.services.llm_service import build_client
from app.services.website_extractor import WebsiteExtractor
from app.utils.cache import TTLCache
from app.utils.rate_limit import RateLimiter

settings = get_settings()
STATIC_DIR = Path(__file__).parent / "static"
EXAMPLE_RESPONSE = json.loads((Path(__file__).parent / "examples" / "link_analysis_response.json").read_text(encoding="utf-8"))

API_DESCRIPTION = """
**Not a developer?** Use the web page instead: **[open the Link Analysis app](/)**. Same analysis, easier to read.

---

This page is the technical documentation for the API.

**How to try it here**

1. Open **POST /link-analysis** below.
2. Replace the URL in the request body, e.g. `{"url": "razorpay.com"}`.
3. Click **Execute** and wait 20-40 seconds.
4. The result appears under **Server response → Response body**.

**What comes back**: `brand` (who the business is) and `analysis` with five sections:
`competitor_analysis`, `target_search_queries`, `seo_improvements`, `top_services`, `target_audience`,
plus `meta` (model, time, cost).

**Reading the schema**: `string` = text, `integer` / `number` = a number, `boolean` = true or false,
`array` = a list, `object` = a group of named fields, `null` = no value, `*` = always present.
"""

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    http = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.fetch_timeout_seconds, connect=8.0),
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
    )
    renderer = JSRenderer(settings) if settings.enable_js_rendering else None

    llm_clients = [build_client(settings.llm_provider, settings.llm_model, http, settings)]
    if settings.llm_fallback_provider and settings.llm_fallback_model:
        llm_clients.append(build_client(settings.llm_fallback_provider, settings.llm_fallback_model, http, settings))

    app.state.analysis_service = AnalysisService(
        extractor=WebsiteExtractor(http, settings, renderer=renderer),
        competitor_service=CompetitorService(http, verify_sites=settings.verify_competitor_sites),
        llm_clients=llm_clients,
        settings=settings,
        cache=TTLCache(settings.cache_ttl_seconds, settings.cache_max_items),
    )
    app.state.rate_limiter = RateLimiter(settings.rate_limit_per_minute)

    logger.info("LLM: %s", " -> ".join(f"{c.provider}/{c.model}" for c in llm_clients))
    logger.info("JavaScript rendering: %s", "enabled" if renderer and renderer.available else "disabled")
    if not any(getattr(settings, f"{c.provider}_api_key", None) for c in llm_clients):
        logger.warning("No API key found for the configured LLM provider(s) - /link-analysis will return 503.")

    yield

    if renderer:
        await renderer.close()
    await http.aclose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=API_DESCRIPTION,
    lifespan=lifespan,
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,  # hide the long "Schemas" list at the bottom
        "docExpansion": "list",
        "displayRequestDuration": True,
        "tryItOutEnabled": True,
    },
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = int((time.perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-ms"] = str(elapsed)
    if request.url.path == "/link-analysis":
        logger.info("[%s] %s %s -> %s in %d ms", request_id, request.method, request.url.path, response.status_code, elapsed)
    return response


def get_analysis_service(request: Request) -> AnalysisService:
    return request.app.state.analysis_service


def rate_limit(request: Request) -> None:
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    client_ip = request.client.host if request.client else "unknown"
    wait = limiter.check(client_ip)
    if wait is not None:
        raise RateLimitError(
            f"Too many requests. Please wait {wait:.0f} seconds and try again.",
            headers={"Retry-After": str(max(int(wait), 1))},
        )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    logger.warning("%s: %s", exc.code, exc.message)
    body = ErrorResponse(error=ErrorDetail(code=exc.code, message=exc.message))
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(mode="json", exclude_none=True),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = [
        {"field": ".".join(str(p) for p in err["loc"] if p != "body"), "message": err["msg"]}
        for err in exc.errors()
    ]
    body = ErrorResponse(
        error=ErrorDetail(code="validation_error", message="Invalid request body.", details=details)
    )
    return JSONResponse(status_code=422, content=body.model_dump(mode="json", exclude_none=True))


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error")
    body = ErrorResponse(error=ErrorDetail(code="internal_error", message="Something went wrong on our side."))
    return JSONResponse(status_code=500, content=body.model_dump(mode="json", exclude_none=True))


def _error_example(description: str, code: str, message: str) -> dict:
    example = {"success": False, "error": {"code": code, "message": message}}
    return {"model": ErrorResponse, "description": description, "content": {"application/json": {"example": example}}}


RESPONSES = {
    200: {
        "description": "The full report. Example below is a real (shortened) result for razorpay.com.",
        "content": {"application/json": {"example": EXAMPLE_RESPONSE}},
    },
    400: _error_example(
        "The link points to a private or internal address (blocked for security)",
        "blocked_url",
        "URLs pointing to private or internal IP addresses are not allowed.",
    ),
    422: _error_example(
        "The link is not valid, or the page has no readable content",
        "invalid_url",
        "URL must contain a full domain name, e.g. https://example.com",
    ),
    429: _error_example(
        "Too many requests from the same person",
        "rate_limited",
        "Too many requests. Please wait 42 seconds and try again.",
    ),
    502: _error_example(
        "The website could not be read (blocked, down, not found) or the model failed",
        "website_access_denied",
        "The website blocked automated access (HTTP 403).",
    ),
    503: _error_example(
        "The server has no model API key configured",
        "llm_not_configured",
        "No LLM API key is configured. Set GEMINI_API_KEY (or another provider key) in the .env file.",
    ),
    504: _error_example(
        "The website or the model took too long",
        "website_timeout",
        "The website took too long to respond.",
    ),
}

REQUEST_EXAMPLES = {
    "razorpay": {"summary": "Razorpay (payments company)", "value": {"url": "https://razorpay.com"}},
    "no_scheme": {"summary": "Without https://", "value": {"url": "basecamp.com"}},
    "javascript_site": {"summary": "A JavaScript-built site", "value": {"url": "https://sorsiri.in"}},
}


@app.post(
    "/link-analysis",
    response_model=LinkAnalysisResponse,
    responses=RESPONSES,
    summary="Analyse a website",
    description=(
        "Reads the website (homepage + up to 3 key pages) and returns a report with five sections: "
        "competitors, top 5 search queries, SEO improvements, top 5 services and target audience. "
        "Takes about 20-40 seconds; the same URL is answered instantly for an hour after that."
    ),
    tags=["Analysis"],
    dependencies=[Depends(rate_limit)],
)
async def link_analysis(
    payload: Annotated[LinkAnalysisRequest, Body(openapi_examples=REQUEST_EXAMPLES)],
    service: AnalysisService = Depends(get_analysis_service),
):
    return await service.analyze(payload.url)


@app.get("/health", tags=["System"], summary="Check the service is running")
async def health():
    return {"status": "ok", "llm_provider": settings.llm_provider, "llm_model": settings.llm_model}


@app.get("/", include_in_schema=False)
async def home():
    return FileResponse(STATIC_DIR / "index.html")
