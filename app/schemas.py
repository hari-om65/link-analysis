from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Platform = Literal["Google", "Facebook", "Instagram", "LinkedIn", "X"]

_PLATFORM_ALIASES = {
    "google": "Google",
    "google search": "Google",
    "facebook": "Facebook",
    "meta": "Facebook",
    "instagram": "Instagram",
    "linkedin": "LinkedIn",
    "x": "X",
    "twitter": "X",
    "x (twitter)": "X",
    "x/twitter": "X",
}


class _Base(BaseModel):
    # LLMs sometimes add extra keys - ignore them instead of failing the whole response
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


def _clean_list(values):
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    seen, result = set(), []
    for value in values:
        if isinstance(value, str):
            key = value.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
        result.append(value)
    return result


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class LinkAnalysisRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048, examples=["https://example.com"], description="The website to analyse. `https://` is optional, e.g. `razorpay.com`.")


# ---------------------------------------------------------------------------
# Section 1 - competitor vs your brand
# ---------------------------------------------------------------------------


class Brand(_Base):
    name: str = Field(description="Brand name as shown on the website.")
    category: str = Field(description="Main business category.")
    summary: str = Field(description="One or two sentences on what the business does.")
    # filled from the website itself, not by the LLM
    social_profiles: dict[str, str] = Field({}, description="Brand's own social media pages found on the site, by platform.")
    contact_emails: list[str] = Field([], description="Email addresses found on the site.")
    contact_phones: list[str] = Field([], description="Phone numbers found on the site.")


class BrandProfile(_Base):
    name: str = Field(description="Brand name.")
    overview: str = Field(description="Short business overview.")
    category: str = Field(description="Main business category.")
    main_products_services: list[str] = Field([], description="Main products and services offered.")
    target_market: str = Field("", description="Who the business sells to.")
    key_strengths: list[str] = Field([], description="What the brand does well.")

    clean_lists = field_validator("main_products_services", "key_strengths", mode="before")(_clean_list)


class Competitor(_Base):
    name: str = Field(description="Competitor company name.")
    website: str | None = Field(None, description="Competitor homepage, or null if not known for sure.")
    main_service: str = Field("", description="What the competitor mainly offers.")
    target_market: str = Field("", description="Who the competitor sells to.")
    strengths: list[str] = Field([], description="Where the competitor is strong.")
    weaknesses: list[str] = Field([], description="Gaps the analysed brand could use to win.")
    confidence: Literal["high", "medium", "low"] = Field("medium", description="How sure the model is that this is a real, relevant competitor.")
    source: Literal["ai_suggestion"] = Field("ai_suggestion", description="Always `ai_suggestion`: competitors come from the model, not a market database.")
    website_verified: bool | None = Field(None, description="true = the competitor's website exists and responds; null = no website given.")

    clean_lists = field_validator("strengths", "weaknesses", mode="before")(_clean_list)

    @field_validator("confidence", mode="before")
    @classmethod
    def lower_confidence(cls, value):
        return value.lower().strip() if isinstance(value, str) else value

    @field_validator("source", mode="before")
    @classmethod
    def force_source(cls, value):
        # Competitors always come from the model, never from a verified data source
        return "ai_suggestion"


class ComparisonRow(_Base):
    aspect: str = Field(description="What is being compared, e.g. Main Service or Target Market.")
    your_brand: str = Field(description="Value for the analysed brand.")
    competitors: dict[str, str] = Field({}, description="Value for each competitor, keyed by competitor name.")


class CompetitorAnalysis(_Base):
    your_brand: BrandProfile = Field(description="Profile of the analysed brand.")
    competitors: list[Competitor] = Field(default_factory=list, description="3-5 suggested competitors.")
    comparison: list[ComparisonRow] = Field([], description="Side-by-side comparison table.")
    competitive_advantages: list[str] = Field([], description="Where the brand is stronger than its competitors.")
    opportunities: list[str] = Field([], description="Concrete ways to improve market positioning.")
    research_note: str | None = Field(None, description="How the competitor list was produced.")

    clean_lists = field_validator("competitive_advantages", "opportunities", mode="before")(_clean_list)

    @field_validator("competitors", mode="after")
    @classmethod
    def limit_competitors(cls, value):
        return value[:6]


# ---------------------------------------------------------------------------
# Section 2 - search queries
# ---------------------------------------------------------------------------


class SearchQuery(_Base):
    query: str = Field(description="What a potential customer would type or search.")
    platforms: list[Platform] = Field(..., min_length=1, description="Where this search makes sense: Google, Facebook, Instagram, LinkedIn or X.")
    intent: str = Field(description="What the person is trying to do, e.g. Service Search or Comparison.")
    business_relevance: str = Field(description="Why this query matters for the business.")
    target_audience: str = Field(description="Who typically searches this.")

    @field_validator("platforms", mode="before")
    @classmethod
    def normalise_platforms(cls, value):
        if isinstance(value, str):
            value = [value]
        result = []
        for item in value or []:
            name = _PLATFORM_ALIASES.get(str(item).strip().lower(), str(item).strip())
            if name not in result:
                result.append(name)
        return result


# ---------------------------------------------------------------------------
# Section 3 - SEO
# ---------------------------------------------------------------------------


class SupportingContent(_Base):
    type: str = Field(description="Kind of content, e.g. Blog topic, FAQ, Service landing page.")
    title: str = Field(description="Suggested title.")
    purpose: str = Field(description="What this content would achieve.")


class SEOAudit(BaseModel):
    """Facts measured directly from the HTML - no LLM involved."""

    current_title: str | None = Field(description="The page title the site uses today.")
    title_length: int = Field(description="Characters in the current title (50-60 is ideal).")
    current_meta_description: str | None = Field(description="The description shown in Google today.")
    meta_description_length: int = Field(description="Characters in the current description (120-160 is ideal).")
    h1_count: int = Field(description="Number of main (H1) headings on the homepage; 1 is ideal.")
    h1_headings: list[str] = Field(description="The H1 headings found.")
    images_total: int = Field(description="Images on the homepage.")
    images_missing_alt: int = Field(description="Images without a text description (alt text).")
    has_canonical: bool = Field(description="Whether the page tells Google its official URL.")
    has_structured_data: bool = Field(description="Whether business details are provided as JSON-LD.")
    has_open_graph: bool = Field(description="Whether social-media preview tags exist.")
    has_viewport_meta: bool = Field(description="Whether the page is set up for mobile screens.")
    has_robots_txt: bool = Field(description="Whether a real robots.txt file exists.")
    sitemap_url: str | None = Field(description="Where the XML sitemap was found, or null.")
    language: str | None = Field(description="Page language from the <html lang> attribute.")
    word_count: int = Field(description="Words of readable text on the homepage.")
    uses_https: bool = Field(description="Whether the site uses a secure connection.")
    rendered_with_javascript: bool = Field(description="true = the content only appears after JavaScript runs.")
    response_time_ms: int = Field(description="How long the homepage HTML took to download.")
    html_size_kb: float = Field(description="Size of the homepage HTML.")
    issues: list[str] = Field(description="Problems found, in plain language.")


class SEOImprovements(_Base):
    primary_keyword: str = Field(description="The main keyword the site should rank for.")
    secondary_keywords: list[str] = Field([], description="Related keywords.")
    long_tail_keywords: list[str] = Field([], description="Longer, more specific searches.")
    business_keywords: list[str] = Field([], description="Product and business terms.")
    recommended_title: str = Field(description="Suggested page title for Google.")
    title_rationale: str = Field("", description="Why the suggested title is better.")
    meta_description: str = Field(description="Suggested description for Google results.")
    supporting_content: list[SupportingContent] = Field([], description="New pages or articles to create.")
    current_seo_audit: SEOAudit | None = Field(None, description="Technical check-up measured on the site (no AI involved).")

    clean_lists = field_validator(
        "secondary_keywords", "long_tail_keywords", "business_keywords", mode="before"
    )(_clean_list)


# ---------------------------------------------------------------------------
# Section 4 - services
# ---------------------------------------------------------------------------


class Service(_Base):
    service_name: str = Field(description="Name of the service.")
    description: str = Field(description="Short explanation.")
    target_industry: str = Field(description="Industry it is aimed at.")
    target_customer: str = Field(description="Type of customer it is for.")
    main_benefit: str = Field(description="Why customers need it.")
    source: Literal["website", "inferred"] = Field("website", description="`website` = listed on the site; `inferred` = likely, but not stated on the site.")

    @field_validator("source", mode="before")
    @classmethod
    def normalise_source(cls, value):
        value = str(value or "").lower()
        return "inferred" if "infer" in value else "website"


# ---------------------------------------------------------------------------
# Section 5 - audience
# ---------------------------------------------------------------------------


class AudienceSegment(_Base):
    audience: str = Field(description="Group of people.")
    business_type: str = Field(description="Type of business they are in.")
    requirement: str = Field(description="What they need.")


class TargetAudience(_Base):
    primary_audience: list[str] = Field(..., min_length=1, description="Main customer groups.")
    secondary_audience: list[str] = Field([], description="Other customer groups.")
    industry_segments: list[str] = Field([], description="Industries served.")
    business_types: list[str] = Field([], description="Types of businesses served.")
    customer_profiles: list[str] = Field([], description="Short descriptions of typical customers.")
    geographic_focus: str | None = Field(None, description="Location focus, or null if the site doesn't say.")
    customer_needs: list[str] = Field([], description="Customer needs and challenges.")
    customer_interests: list[str] = Field([], description="Topics customers care about.")
    recommended_segments: list[AudienceSegment] = Field([], description="Groups to target in marketing.")

    clean_lists = field_validator(
        "primary_audience",
        "secondary_audience",
        "industry_segments",
        "business_types",
        "customer_profiles",
        "customer_needs",
        "customer_interests",
        mode="before",
    )(_clean_list)


# ---------------------------------------------------------------------------
# Full analysis
# ---------------------------------------------------------------------------


class Analysis(_Base):
    competitor_analysis: CompetitorAnalysis = Field(description="Section 1: competitors vs. the brand.")
    target_search_queries: list[SearchQuery] = Field(..., min_length=5, description="Section 2: top 5 search queries.")
    seo_improvements: SEOImprovements = Field(description="Section 3: SEO improvements.")
    top_services: list[Service] = Field(..., min_length=1, description="Section 4: top 5 services.")
    target_audience: TargetAudience = Field(description="Section 5: target audience.")

    @field_validator("target_search_queries", "top_services", mode="after")
    @classmethod
    def keep_top_five(cls, value):
        return value[:5]


class LLMAnalysisOutput(_Base):
    """Exact shape we ask the LLM to return."""

    brand: Brand
    analysis: Analysis


class AnalysisMeta(BaseModel):
    llm_provider: str = Field(description="Which provider wrote the report.")
    llm_model: str = Field(description="Which model wrote the report.")
    used_fallback: bool = Field(False, description="true = the main model failed and the backup model answered.")
    pages_analyzed: list[str] = Field(description="Pages that were read.")
    input_tokens: int | None = Field(None, description="Tokens sent to the model.")
    output_tokens: int | None = Field(None, description="Tokens the model produced (including thinking).")
    estimated_cost_usd: float | None = Field(None, description="Cost of this analysis in US dollars.")
    llm_latency_ms: int | None = Field(None, description="Time the model took.")
    total_duration_ms: int = Field(description="Total time for the request.")
    cached: bool = Field(False, description="true = a saved result from the last hour was returned (no cost).")
    generated_at: datetime = Field(description="When the analysis was produced (UTC).")
    notes: list[str] = Field([], description="Things worth knowing about this result.")


class LinkAnalysisResponse(BaseModel):
    success: Literal[True] = Field(True, description="Always true for a successful analysis.")
    website: str = Field(description="Final URL that was analysed (after redirects).")
    brand: Brand = Field(description="Who the business is.")
    analysis: Analysis = Field(description="The five report sections.")
    meta: AnalysisMeta = Field(description="How the analysis was produced: model, time, cost.")


class ErrorDetail(BaseModel):
    code: str = Field(description="Machine-readable error code, e.g. `blocked_url`.")
    message: str = Field(description="Human-readable explanation.")
    details: list | dict | None = Field(None, description="Extra information, e.g. which field is invalid.")


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    error: ErrorDetail
