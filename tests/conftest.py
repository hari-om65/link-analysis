import copy
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app, get_analysis_service
from app.services.analysis_service import AnalysisService
from app.services.competitor_service import CompetitorService
from app.services.llm_service import BaseLLMClient, LLMResult
from app.services.website_extractor import WebsiteExtractor
from app.utils import validators
from app.utils.cache import TTLCache

FIXTURES = Path(__file__).parent / "fixtures"
HOMEPAGE = (FIXTURES / "homepage.html").read_text(encoding="utf-8")
LLM_OUTPUT = json.loads((FIXTURES / "llm_output.json").read_text(encoding="utf-8"))

ABOUT_PAGE = """<html><head><title>About GreenNest</title></head><body><main>
<p>GreenNest Interiors was founded in 2015 by two architects who wanted homes to be healthier.
We have completed more than 400 projects across Pune using low-VOC paints and reclaimed wood.</p>
</main></body></html>"""

SERVICES_PAGE = """<html><head><title>Our Services</title></head><body><main>
<h2>Turnkey home interiors</h2><p>From concept to handover, our team manages design, procurement and execution.</p>
<h2>Commercial fit-outs</h2><p>Office and retail fit-outs for growing businesses with fixed timelines.</p>
</main></body></html>"""

ROBOTS_TXT = "User-agent: *\nDisallow: /admin\nSitemap: https://greennest.example/sitemap.xml\n"
SITEMAP = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://greennest.example/</loc></url></urlset>'

SPA_SHELL = """<!doctype html><html lang="en"><head><title>Orbit | Team Scheduling</title>
<meta name="description" content="Orbit helps teams plan shifts.">
<meta name="viewport" content="width=device-width"></head>
<body><div id="root"></div><script type="module" src="/assets/index.js"></script></body></html>"""

SPA_RENDERED = {
    "/": """<html lang="en"><head><title>Orbit | Team Scheduling</title></head><body>
<nav><a href="/features">Features</a><a href="/pricing">Pricing</a><a href="https://www.linkedin.com/company/orbit-app">LinkedIn</a></nav>
<main><h1>Shift scheduling for restaurants and clinics</h1>
<p>Orbit builds weekly rotas automatically, handles shift swaps and tracks overtime for teams of 5 to 500 people.</p>
<p>Managers save around six hours a week and staff get their schedule on their phone.</p>
<h2>Built for hospitality and healthcare</h2>
<p>Integrates with payroll tools and supports multiple locations, availability rules and labour law limits.</p>
</main></body></html>""",
    "/features": """<html><head><title>Features</title></head><body><main>
<h2>Auto scheduling</h2><p>Generate a fair rota in one click based on availability, skills and labour rules for every location.</p>
</main></body></html>""",
    "/pricing": """<html><head><title>Pricing</title></head><body><main>
<h2>Plans</h2><p>Free for up to 10 staff, then 2 dollars per user per month with unlimited locations included.</p>
<p>Annual billing gets two months free and every plan includes email support.</p>
</main></body></html>""",
}


class FakeRenderer:
    """Stands in for headless Chromium."""

    available = True

    def __init__(self):
        self.rendered = []

    async def render(self, url):
        self.rendered.append(url)
        parsed = httpx.URL(url)
        if parsed.host != "spa.example":
            return url, "<html><body><div id='root'></div></body></html>"  # still empty after JS
        return url, SPA_RENDERED.get(parsed.path, SPA_RENDERED["/"])


# Fake DNS: everything resolves to a public IP unless listed here
PRIVATE_HOSTS = {"internal.example": "10.0.0.7", "rebind.example": "127.0.0.1"}


def site_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    html = {"content-type": "text/html; charset=utf-8"}

    if host == "greennest.example":
        pages = {"/": HOMEPAGE, "/about-us": ABOUT_PAGE, "/services": SERVICES_PAGE}
        if path in pages:
            return httpx.Response(200, text=pages[path], headers=html)
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_TXT, headers={"content-type": "text/plain"})
        if path == "/sitemap.xml":
            return httpx.Response(200, text=SITEMAP, headers={"content-type": "application/xml"})
        return httpx.Response(404, text="not found", headers=html)
    if host == "spa.example":
        # React-style app: the server only sends an empty shell, for every route
        return httpx.Response(200, text=SPA_SHELL, headers=html)
    if host == "www.greennest.example":
        return httpx.Response(301, headers={"location": "https://greennest.example/"})
    if host == "redirect-to-internal.example":
        return httpx.Response(302, headers={"location": "http://10.0.0.1/admin"})
    if host == "blocked.example":
        return httpx.Response(403, text="Forbidden")
    if host == "json.example":
        return httpx.Response(200, json={"hello": "world"})
    if host == "empty.example":
        return httpx.Response(200, text="<html><body><div id='root'></div></body></html>", headers=html)
    if host == "slow.example":
        raise httpx.ReadTimeout("timed out", request=request)
    if host in ("livspace.com", "www.livspace.com"):
        return httpx.Response(200, text="<html></html>", headers=html)
    if host == "www.homelane.com":
        return httpx.Response(301, headers={"location": "https://www.homelane.com/in"})
    return httpx.Response(404)


class FakeLLM(BaseLLMClient):
    """Returns queued payloads instead of calling a real provider."""

    def __init__(self, responses, provider="fake", model="gemini-3.5-flash-lite"):
        self.provider = provider
        self.model = model
        self.responses = list(responses)
        self.calls = []

    async def generate_json(self, system_prompt, user_prompt):
        self.calls.append(user_prompt)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return LLMResult(
            data=copy.deepcopy(item),
            provider=self.provider,
            model=self.model,
            input_tokens=4200,
            output_tokens=2800,
            latency_ms=1234,
        )


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    async def resolve(host, port):
        return [PRIVATE_HOSTS.get(host, "93.184.216.34")]

    monkeypatch.setattr(validators, "resolve_host", resolve)


@pytest.fixture
def settings():
    return Settings(_env_file=None, cache_ttl_seconds=600, max_extra_pages=3)


@pytest.fixture
def http_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(site_handler))


@pytest.fixture
def llm_output():
    return copy.deepcopy(LLM_OUTPUT)


@pytest.fixture
def renderer():
    return FakeRenderer()


@pytest.fixture
def make_service(settings, http_client, renderer):
    def _make(*llm_clients):
        return AnalysisService(
            extractor=WebsiteExtractor(http_client, settings, renderer=renderer),
            competitor_service=CompetitorService(http_client, verify_sites=True),
            llm_clients=list(llm_clients),
            settings=settings,
            cache=TTLCache(settings.cache_ttl_seconds),
        )

    return _make


@pytest.fixture
def api(make_service):
    """Returns a factory: api(llm1, llm2, ...) -> TestClient wired to those fake LLMs."""
    clients = []

    def _api(*llm_clients):
        service = make_service(*llm_clients)
        app.dependency_overrides[get_analysis_service] = lambda: service
        client = TestClient(app)
        clients.append(client)
        return client

    yield _api
    app.dependency_overrides.clear()
