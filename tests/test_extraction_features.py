import asyncio

import pytest

from app.exceptions import BlockedURLError
from app.main import app
from app.services.js_renderer import JSRenderer
from app.services.website_extractor import WebsiteExtractor
from app.utils.rate_limit import RateLimiter
from tests.conftest import FakeLLM

URL = "https://greennest.example"


# ---------------------------------------------------------------------------
# JavaScript-rendered sites


async def test_spa_is_rendered_with_browser(http_client, settings, renderer):
    content = await WebsiteExtractor(http_client, settings, renderer=renderer).extract("https://spa.example")

    assert content.rendered_with_javascript is True
    assert "Shift scheduling for restaurants" in content.pages[0].text
    assert content.headings["h1"] == ["Shift scheduling for restaurants and clinics"]
    # sub-pages of a JS site are rendered too
    assert "https://spa.example/features" in content.pages_analyzed
    assert "https://spa.example/pricing" in content.pages_analyzed
    assert any("JavaScript" in issue for issue in content.seo_audit.issues)
    assert content.seo_audit.rendered_with_javascript is True
    # meta tags still come from the raw HTML, which is what crawlers read
    assert content.meta_description == "Orbit helps teams plan shifts."


async def test_normal_site_is_not_rendered(http_client, settings, renderer):
    content = await WebsiteExtractor(http_client, settings, renderer=renderer).extract(URL)
    assert content.rendered_with_javascript is False
    assert renderer.rendered == []


async def test_spa_without_browser_falls_back_to_meta_tags(http_client, settings):
    content = await WebsiteExtractor(http_client, settings, renderer=None).extract("https://spa.example")
    assert content.rendered_with_javascript is False
    assert content.title == "Orbit | Team Scheduling"
    assert any("JavaScript" in warning for warning in content.warnings)


async def test_rendering_can_be_switched_off(http_client, settings, renderer):
    settings.enable_js_rendering = False
    content = await WebsiteExtractor(http_client, settings, renderer=renderer).extract("https://spa.example")
    assert content.rendered_with_javascript is False
    assert renderer.rendered == []


async def test_browser_requests_to_internal_addresses_are_refused():
    assert await JSRenderer.is_allowed("https://cdn.example/app.js") is True
    assert await JSRenderer.is_allowed("http://127.0.0.1:8080/admin") is False
    assert await JSRenderer.is_allowed("http://169.254.169.254/latest/meta-data") is False
    assert await JSRenderer.is_allowed("https://internal.example/api") is False  # resolves to 10.x


async def test_renderer_rejects_private_url_before_starting_browser(settings):
    with pytest.raises(BlockedURLError):
        await JSRenderer(settings).render("http://192.168.0.10/")


def test_spa_end_to_end(api, llm_output):
    llm = FakeLLM([llm_output])
    body = api(llm).post("/link-analysis", json={"url": "https://spa.example"}).json()

    assert body["success"] is True
    assert "Orbit builds weekly rotas" in llm.calls[0]
    assert body["analysis"]["seo_improvements"]["current_seo_audit"]["rendered_with_javascript"] is True
    assert any("headless browser" in note for note in body["meta"]["notes"])


# ---------------------------------------------------------------------------
# Social profiles, contacts, technical SEO


async def test_social_profiles_and_contacts(http_client, settings):
    content = await WebsiteExtractor(http_client, settings).extract(URL)

    assert content.social_profiles == {
        "LinkedIn": "https://www.linkedin.com/company/greennest-interiors",
        "Instagram": "https://instagram.com/greennest",
    }  # the Facebook "share" button is not a profile
    assert content.contact_emails == ["hello@greennest.example"]
    assert content.contact_phones == ["+919000000000"]
    assert "Brand social profiles found on the site: LinkedIn, Instagram" in content.to_prompt(10_000)


def test_brand_contains_social_and_contacts(api, llm_output):
    body = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": URL}).json()
    brand = body["brand"]
    assert brand["social_profiles"]["Instagram"] == "https://instagram.com/greennest"
    assert brand["contact_emails"] == ["hello@greennest.example"]


async def test_robots_sitemap_and_viewport(http_client, settings):
    audit = (await WebsiteExtractor(http_client, settings).extract(URL)).seo_audit
    assert audit.has_robots_txt is True
    assert audit.sitemap_url == "https://greennest.example/sitemap.xml"
    assert audit.has_viewport_meta is True
    assert audit.response_time_ms >= 0
    assert audit.html_size_kb > 0
    assert not any("robots.txt" in issue or "sitemap" in issue.lower() for issue in audit.issues)


async def test_missing_robots_and_sitemap_are_reported(http_client, settings, renderer):
    audit = (await WebsiteExtractor(http_client, settings, renderer=renderer).extract("https://spa.example")).seo_audit
    assert audit.has_robots_txt is False
    assert audit.sitemap_url is None
    assert "No robots.txt file found." in audit.issues


# ---------------------------------------------------------------------------
# API behaviour


def test_identical_concurrent_requests_share_one_llm_call(make_service, llm_output):
    class SlowLLM(FakeLLM):
        async def generate_json(self, system_prompt, user_prompt):
            await asyncio.sleep(0.2)
            return await super().generate_json(system_prompt, user_prompt)

    llm = SlowLLM([llm_output])
    service = make_service(llm)

    async def run():
        return await asyncio.gather(*(service.analyze(URL) for _ in range(3)))

    results = asyncio.run(run())
    assert len(llm.calls) == 1
    assert sorted(r.meta.cached for r in results) == [False, True, True]


def test_rate_limit(api, llm_output):
    client = api(FakeLLM([llm_output]))
    app.state.rate_limiter = RateLimiter(max_requests=2)
    try:
        assert client.post("/link-analysis", json={"url": URL}).status_code == 200
        assert client.post("/link-analysis", json={"url": URL}).status_code == 200
        blocked = client.post("/link-analysis", json={"url": URL})
    finally:
        del app.state.rate_limiter

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1


def test_request_id_and_timing_headers(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": URL}, headers={"X-Request-ID": "abc123"})
    assert response.headers["x-request-id"] == "abc123"
    assert int(response.headers["x-response-time-ms"]) >= 0


def test_web_ui_is_served(api, llm_output):
    client = api(FakeLLM([llm_output]))
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="analyse-form"' in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_social_profile_prefers_company_pages_over_posts_and_people():
    from app.services.website_extractor import _social_profiles

    links = [
        ("https://www.instagram.com/reel/DamjVinlMuU/", "watch"),
        ("https://www.linkedin.com/in/founder-name", "Founder"),
        ("https://www.linkedin.com/company/acme-labs/", "LinkedIn"),
        ("https://x.com/acme/status/123456", "tweet"),
        ("https://x.com/acme", "X"),
        ("https://www.youtube.com/watch?v=abc", "video"),
    ]
    assert _social_profiles(links, {}) == {
        "LinkedIn": "https://www.linkedin.com/company/acme-labs",
        "X": "https://x.com/acme",
    }
