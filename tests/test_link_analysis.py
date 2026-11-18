from app.exceptions import LLMError, LLMNotConfiguredError

from tests.conftest import FakeLLM

URL = "https://greennest.example"


def test_successful_analysis_returns_all_five_sections(api, llm_output):
    llm = FakeLLM([llm_output])
    response = api(llm).post("/link-analysis", json={"url": URL})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["website"] == "https://greennest.example/"
    assert body["brand"]["name"] == "GreenNest Interiors"

    analysis = body["analysis"]
    assert set(analysis) == {
        "competitor_analysis",
        "target_search_queries",
        "seo_improvements",
        "top_services",
        "target_audience",
    }
    assert len(analysis["target_search_queries"]) == 5
    assert len(analysis["top_services"]) == 5


def test_website_content_is_sent_to_the_llm(api, llm_output):
    llm = FakeLLM([llm_output])
    api(llm).post("/link-analysis", json={"url": URL})

    prompt = llm.calls[0]
    assert "Sustainable interiors for modern homes" in prompt  # H1 from the homepage
    assert "founded in 2015" in prompt  # text from the /about-us page
    assert "Turnkey home interiors" in prompt  # text from the /services page
    assert "console.log" not in prompt  # scripts are stripped
    assert "We use cookies" not in prompt  # cookie banner is stripped


def test_extra_pages_are_reported_in_meta(api, llm_output):
    body = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": URL}).json()
    pages = body["meta"]["pages_analyzed"]
    assert pages[0] == "https://greennest.example/"
    assert "https://greennest.example/about-us" in pages
    assert "https://greennest.example/services" in pages
    assert not any("login" in page for page in pages)


def test_competitors_are_labelled_verified_and_deduplicated(api, llm_output):
    body = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": URL}).json()
    section = body["analysis"]["competitor_analysis"]
    competitors = {c["name"]: c for c in section["competitors"]}

    assert "GreenNest Interiors" not in competitors  # the brand itself is removed
    assert all(c["source"] == "ai_suggestion" for c in competitors.values())
    assert competitors["Livspace"]["website"] == "https://livspace.com"
    assert competitors["Livspace"]["website_verified"] is True
    assert competitors["Livspace"]["confidence"] == "high"
    assert competitors["HomeLane"]["website_verified"] is True  # 301 still proves it exists
    assert competitors["Design Cafe"]["website_verified"] is None
    assert "AI-generated" in section["research_note"]


def test_search_queries_use_only_supported_platforms(api, llm_output):
    body = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": URL}).json()
    platforms = {p for q in body["analysis"]["target_search_queries"] for p in q["platforms"]}
    assert platforms <= {"Google", "Facebook", "Instagram", "LinkedIn", "X"}
    assert "X" in platforms  # "Twitter" got normalised
    assert any("search volume" in note.lower() for note in body["meta"]["notes"])


def test_seo_section_includes_measured_audit(api, llm_output):
    body = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": URL}).json()
    audit = body["analysis"]["seo_improvements"]["current_seo_audit"]
    assert audit["current_title"] == "GreenNest Interiors"
    assert audit["h1_count"] == 1
    assert audit["images_missing_alt"] == 1
    assert audit["has_structured_data"] is True
    assert any("Title is short" in issue for issue in audit["issues"])


def test_inferred_services_are_labelled(api, llm_output):
    body = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": URL}).json()
    sources = [s["source"] for s in body["analysis"]["top_services"]]
    assert sources.count("inferred") == 2
    assert any("inferred" in note for note in body["meta"]["notes"])


def test_meta_contains_usage_and_cost(api, llm_output):
    body = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": URL}).json()
    meta = body["meta"]
    assert meta["input_tokens"] == 4200
    assert meta["output_tokens"] == 2800
    # 4200 * 0.30 / 1M + 2800 * 2.50 / 1M
    assert meta["estimated_cost_usd"] == 0.00826
    assert meta["cached"] is False


def test_second_request_is_served_from_cache(api, llm_output):
    llm = FakeLLM([llm_output])
    client = api(llm)
    client.post("/link-analysis", json={"url": URL})
    second = client.post("/link-analysis", json={"url": "greennest.example"}).json()

    assert second["meta"]["cached"] is True
    assert len(llm.calls) == 1


def test_invalid_llm_output_is_repaired(api, llm_output):
    broken = {"brand": llm_output["brand"], "analysis": {"target_search_queries": []}}
    llm = FakeLLM([broken, llm_output])
    response = api(llm).post("/link-analysis", json={"url": URL})

    assert response.status_code == 200
    assert len(llm.calls) == 2
    assert "Validation errors" in llm.calls[1]
    assert response.json()["meta"]["input_tokens"] == 8400  # both calls are counted


def test_output_that_cannot_be_repaired_returns_error(api):
    llm = FakeLLM([{"brand": {"name": "x"}}])
    response = api(llm).post("/link-analysis", json={"url": URL})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_invalid_output"


def test_fallback_model_is_used_when_primary_fails(api, llm_output):
    primary = FakeLLM([LLMError("quota exceeded")])
    fallback = FakeLLM([llm_output], provider="openai", model="gpt-5-mini")
    body = api(primary, fallback).post("/link-analysis", json={"url": URL}).json()

    assert body["meta"]["llm_model"] == "gpt-5-mini"
    assert body["meta"]["used_fallback"] is True


def test_missing_api_key_returns_503(api):
    llm = FakeLLM([LLMNotConfiguredError("GEMINI_API_KEY is not set.")])
    response = api(llm).post("/link-analysis", json={"url": URL})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "llm_not_configured"


def test_primary_error_is_reported_when_fallback_has_no_key(api):
    primary = FakeLLM([LLMError("gemini request timed out.", code="llm_timeout", status_code=504)])
    fallback = FakeLLM([LLMNotConfiguredError("API key for openai is not set.")], provider="openai")
    response = api(primary, fallback).post("/link-analysis", json={"url": URL})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "llm_timeout"


def test_llm_failure_returns_502(api):
    llm = FakeLLM([LLMError("gemini returned HTTP 500")])
    response = api(llm).post("/link-analysis", json={"url": URL})
    assert response.status_code == 502
    assert response.json()["success"] is False


# ---------------------------------------------------------------------------
# Input validation and website errors
# ---------------------------------------------------------------------------


def test_missing_url_field(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_invalid_url(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "not a url"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_url"


def test_unsupported_scheme(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "ftp://greennest.example"})
    assert response.status_code == 422


def test_private_ip_is_blocked(api, llm_output):
    llm = FakeLLM([llm_output])
    for url in ("http://127.0.0.1", "http://192.168.1.10/admin", "http://169.254.169.254/latest/meta-data"):
        response = api(llm).post("/link-analysis", json={"url": url})
        assert response.status_code == 400, url
        assert response.json()["error"]["code"] == "blocked_url"
    assert llm.calls == []


def test_domain_resolving_to_private_ip_is_blocked(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "https://internal.example"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "blocked_url"


def test_redirect_to_internal_address_is_blocked(api, llm_output):
    response = api(FakeLLM([llm_output])).post(
        "/link-analysis", json={"url": "https://redirect-to-internal.example"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "blocked_url"


def test_redirect_is_followed(api, llm_output):
    body = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "https://www.greennest.example"}).json()
    assert body["website"] == "https://greennest.example/"


def test_site_that_blocks_bots(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "https://blocked.example"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "website_access_denied"


def test_site_timeout(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "https://slow.example"})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "website_timeout"


def test_non_html_content(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "https://json.example"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "content_not_usable"


def test_empty_javascript_page(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "https://empty.example"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "content_not_usable"


def test_page_not_found(api, llm_output):
    response = api(FakeLLM([llm_output])).post("/link-analysis", json={"url": "https://greennest.example/missing"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "website_not_found"


def test_health(api, llm_output):
    response = api(FakeLLM([llm_output])).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
