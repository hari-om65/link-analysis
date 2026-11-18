class AppError(Exception):
    """Base error that maps cleanly to a JSON error response."""

    status_code = 500
    code = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.headers = headers or {}
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class InvalidURLError(AppError):
    status_code = 422
    code = "invalid_url"


class BlockedURLError(AppError):
    # URL points at a private / internal address (SSRF protection)
    status_code = 400
    code = "blocked_url"


class WebsiteFetchError(AppError):
    status_code = 502
    code = "website_unreachable"


class WebsiteTimeoutError(AppError):
    status_code = 504
    code = "website_timeout"


class ContentNotUsableError(AppError):
    status_code = 422
    code = "content_not_usable"


class LLMError(AppError):
    status_code = 502
    code = "llm_error"


class LLMNotConfiguredError(AppError):
    status_code = 503
    code = "llm_not_configured"


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"
