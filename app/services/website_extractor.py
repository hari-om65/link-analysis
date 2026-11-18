"""Fetches a website and turns it into compact, LLM-friendly text.

The goal is to send the model as little as possible while keeping everything
that says what the business does: title/meta, headings, navigation labels,
JSON-LD organisation data, social profiles, and the readable text of the
homepage plus a few key internal pages (about, services, products, pricing...).

If the static HTML is nearly empty (typical for React/Vue/Angular sites) the
page is rendered in headless Chromium instead - see js_renderer.py.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from app.config import Settings
from app.exceptions import (
    AppError,
    ContentNotUsableError,
    WebsiteFetchError,
    WebsiteTimeoutError,
)
from app.schemas import SEOAudit
from app.utils.validators import ensure_public_url, same_site

logger = logging.getLogger(__name__)

NOISE_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "canvas", "form", "button", "select"]
LAYOUT_TAGS = ["header", "nav", "footer", "aside"]
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".mp3", ".doc", ".docx", ".xls", ".xlsx")

# Path / anchor words that usually lead to pages describing the business
PAGE_KEYWORDS = {
    "about": 6,
    "service": 6,
    "solution": 5,
    "product": 5,
    "what-we-do": 5,
    "offering": 4,
    "industr": 4,
    "pricing": 4,
    "plans": 3,
    "feature": 3,
    "company": 3,
    "who-we-are": 4,
    "platform": 3,
    "capabilit": 3,
    "expertise": 3,
}
LOW_VALUE_PAGES = ("login", "signin", "sign-in", "signup", "register", "cart", "checkout", "privacy", "terms", "cookie", "careers", "jobs", "blog/", "/tag/", "/category/", "wp-admin", "account")

SOCIAL_DOMAINS = {
    "facebook.com": "Facebook",
    "instagram.com": "Instagram",
    "linkedin.com": "LinkedIn",
    "x.com": "X",
    "twitter.com": "X",
    "youtube.com": "YouTube",
    "tiktok.com": "TikTok",
    "pinterest.com": "Pinterest",
    "threads.net": "Threads",
}
# share buttons are not the brand's own profile
SOCIAL_SHARE_HINTS = ("sharer", "/share", "intent/tweet", "shareArticle", "/sharing/", "/dialog/")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
FILE_LIKE_EMAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|js|css)$", re.I)


@dataclass
class PageText:
    url: str
    title: str
    text: str


@dataclass
class ParsedDocument:
    """Everything we pull out of one HTML document."""

    head: dict
    organization: dict
    links: list[tuple[str, str]]
    nav_items: list[str]
    footer_text: str
    headings: dict[str, list[str]]
    text: str
    audit_counts: dict
    social_profiles: dict[str, str]
    emails: list[str]
    phones: list[str]
    looks_like_js_app: bool


@dataclass
class FetchResult:
    final_url: str
    body: bytes
    encoding: str | None
    content_type: str
    elapsed_ms: int


@dataclass
class WebsiteContent:
    requested_url: str
    final_url: str
    site_name: str | None
    title: str | None
    meta_description: str | None
    meta_keywords: str | None
    language: str | None
    headings: dict[str, list[str]]
    nav_items: list[str]
    organization: dict
    footer_text: str
    social_profiles: dict[str, str] = field(default_factory=dict)
    contact_emails: list[str] = field(default_factory=list)
    contact_phones: list[str] = field(default_factory=list)
    rendered_with_javascript: bool = False
    pages: list[PageText] = field(default_factory=list)
    seo_audit: SEOAudit | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def pages_analyzed(self) -> list[str]:
        return [page.url for page in self.pages]

    def to_prompt(self, max_chars: int) -> str:
        """Build the text block that goes to the LLM, trimmed to a char budget."""
        lines = [f"Website URL: {self.final_url}"]
        if self.site_name:
            lines.append(f"Site name: {self.site_name}")
        if self.title:
            lines.append(f"Page title: {self.title}")
        if self.meta_description:
            lines.append(f"Meta description: {self.meta_description}")
        if self.meta_keywords:
            lines.append(f"Meta keywords: {self.meta_keywords}")
        if self.language:
            lines.append(f"Language: {self.language}")
        if self.organization:
            lines.append(f"Structured data (JSON-LD): {json.dumps(self.organization, ensure_ascii=False)[:800]}")
        if self.social_profiles:
            lines.append("Brand social profiles found on the site: " + ", ".join(self.social_profiles))
        for level in ("h1", "h2", "h3"):
            if self.headings.get(level):
                lines.append(f"{level.upper()} headings: " + " | ".join(self.headings[level][:15]))
        if self.nav_items:
            lines.append("Navigation menu: " + " | ".join(self.nav_items[:40]))
        if self.footer_text:
            lines.append(f"Footer / contact info: {self.footer_text[:600]}")

        header = "\n".join(lines)
        remaining = max(max_chars - len(header), 1500)

        # Homepage gets half the budget, the other pages share the rest
        blocks = []
        if self.pages:
            home, others = self.pages[0], self.pages[1:]
            home_budget = remaining if not others else remaining // 2
            blocks.append(_page_block(home, home_budget, label="Homepage"))
            if others:
                share = (remaining - home_budget) // len(others)
                for page in others:
                    blocks.append(_page_block(page, share))

        return header + "\n\n" + "\n\n".join(blocks)


def _page_block(page: PageText, budget: int, label: str | None = None) -> str:
    path = urlsplit(page.url).path or "/"
    heading = f"--- {label or 'Page'}: {path}" + (f" ({page.title})" if page.title else "") + " ---"
    text = page.text
    if len(text) > budget:
        text = text[:budget].rsplit(" ", 1)[0] + " ..."
    return f"{heading}\n{text}"


class WebsiteExtractor:
    def __init__(self, client: httpx.AsyncClient, settings: Settings, renderer=None):
        self.client = client
        self.settings = settings
        self.renderer = renderer

    @property
    def can_render(self) -> bool:
        return bool(self.settings.enable_js_rendering and self.renderer and self.renderer.available)

    async def extract(self, url: str) -> WebsiteContent:
        home = await self.fetch(url)
        final_url = home.final_url
        doc = parse_document(home.body, home.encoding, final_url)
        static_doc = doc
        rendered = False

        # Only pay for a browser when the page really looks like a client-side app
        thin = len(doc.text) < self.settings.js_render_min_chars
        if thin and (doc.looks_like_js_app or len(doc.text) < 100) and self.can_render:
            try:
                final_url, html = await self.renderer.render(final_url)
                rendered_doc = parse_document(html.encode("utf-8"), "utf-8", final_url)
                if len(rendered_doc.text) > len(doc.text):
                    doc, rendered = rendered_doc, True
                    logger.info("Rendered %s with JavaScript: %d chars of text", final_url, len(doc.text))
            except AppError as exc:
                logger.info("JavaScript rendering failed for %s: %s", final_url, exc.message)

        # Search engines read the static <head>, so prefer it when it has values
        head = {key: static_doc.head.get(key) or doc.head.get(key) for key in doc.head}
        content = WebsiteContent(
            requested_url=url,
            final_url=final_url,
            site_name=head["site_name"] or doc.organization.get("name"),
            title=head["title"],
            meta_description=head["description"],
            meta_keywords=head["keywords"],
            language=head["language"],
            headings=doc.headings,
            nav_items=doc.nav_items,
            organization=doc.organization or static_doc.organization,
            footer_text=doc.footer_text,
            social_profiles={**static_doc.social_profiles, **doc.social_profiles},
            contact_emails=_merge(static_doc.emails, doc.emails)[:3],
            contact_phones=_merge(static_doc.phones, doc.phones)[:3],
            rendered_with_javascript=rendered,
            pages=[PageText(url=final_url, title=head["title"] or "", text=doc.text)],
        )

        extra_limit = self.settings.js_render_extra_pages if rendered else self.settings.max_extra_pages
        extra_urls = _pick_extra_pages(doc.links, final_url, extra_limit)

        extra_pages, (has_robots, sitemap_url) = await asyncio.gather(
            self._fetch_extra_pages(extra_urls, use_browser=rendered),
            self._check_crawl_files(final_url),
        )
        content.pages.extend(extra_pages)

        content.seo_audit = _build_seo_audit(
            content,
            static_doc=static_doc,
            rendered_doc=doc if rendered else None,
            home=home,
            has_robots=has_robots,
            sitemap_url=sitemap_url,
        )

        total_text = sum(len(page.text) for page in content.pages)
        if total_text < 200:
            if not (content.title or content.meta_description or content.headings.get("h1")):
                raise ContentNotUsableError(
                    "The website returned almost no readable content. It may require JavaScript "
                    "rendering or block automated access."
                )
            content.warnings.append(
                "Very little readable text was found (the site may be rendered with JavaScript). "
                "Analysis relies mostly on title, meta tags and headings."
            )
        if rendered:
            content.warnings.append(
                "The site builds its content with JavaScript, so it was rendered in a headless browser before analysis."
            )

        logger.info(
            "Extracted %s: %d page(s), %d chars of text%s",
            final_url,
            len(content.pages),
            total_text,
            " (JS rendered)" if rendered else "",
        )
        return content

    async def fetch(self, url: str, *, html_only: bool = True) -> FetchResult:
        """GET a page, following redirects by hand so every hop is SSRF-checked."""
        current = url
        started = time.perf_counter()
        for _ in range(self.settings.max_redirects + 1):
            current = await ensure_public_url(current)
            try:
                request = self.client.build_request("GET", current)
                response = await self.client.send(request, stream=True, follow_redirects=False)
            except httpx.TimeoutException:
                raise WebsiteTimeoutError("The website took too long to respond.") from None
            except httpx.HTTPError as exc:
                raise WebsiteFetchError(f"Could not connect to the website ({exc.__class__.__name__}).") from None

            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise WebsiteFetchError("The website returned a redirect without a location.")
                    current = urljoin(current, location)
                    continue

                if response.status_code in (401, 403, 429):
                    raise WebsiteFetchError(
                        f"The website blocked automated access (HTTP {response.status_code}).",
                        code="website_access_denied",
                    )
                if response.status_code == 404:
                    raise WebsiteFetchError("The page was not found (HTTP 404).", code="website_not_found")
                if response.status_code >= 400:
                    raise WebsiteFetchError(f"The website responded with HTTP {response.status_code}.")

                content_type = response.headers.get("content-type", "").lower()
                if html_only and content_type and "html" not in content_type and "xml" not in content_type:
                    raise ContentNotUsableError(
                        f"The URL does not point to an HTML page (content type: {content_type.split(';')[0]})."
                    )

                body = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) >= self.settings.max_page_bytes:
                            break  # good enough - the useful part of a page is near the top
                except httpx.TimeoutException:
                    if not body:
                        raise WebsiteTimeoutError("The website took too long to respond.") from None
                except httpx.HTTPError:
                    if not body:
                        raise WebsiteFetchError("The connection was closed before any content was received.") from None

                return FetchResult(
                    final_url=str(response.url),
                    body=bytes(body),
                    encoding=response.charset_encoding,
                    content_type=content_type,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )
            finally:
                await response.aclose()

        raise WebsiteFetchError("The website redirected too many times.")

    async def _fetch_extra_pages(self, urls: list[str], use_browser: bool = False) -> list[PageText]:
        async def grab(page_url: str) -> PageText | None:
            try:
                if use_browser:
                    final_url, html = await self.renderer.render(page_url)
                    body, encoding = html.encode("utf-8"), "utf-8"
                else:
                    result = await self.fetch(page_url)
                    final_url, body, encoding = result.final_url, result.body, result.encoding
            except AppError as exc:
                logger.debug("Skipping %s: %s", page_url, exc)
                return None
            if not same_site(final_url, page_url):
                return None
            soup = BeautifulSoup(body, "lxml", from_encoding=encoding)
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            _strip_noise(soup)
            text = _readable_text(soup)
            return PageText(url=final_url, title=title, text=text) if len(text) > 100 else None

        results = await asyncio.gather(*(grab(u) for u in urls))
        pages, seen_text = [], set()
        for page in results:
            if page is None:
                continue
            fingerprint = page.text[:300]
            if fingerprint in seen_text:
                continue
            seen_text.add(fingerprint)
            pages.append(page)
        return pages

    async def _check_crawl_files(self, site_url: str) -> tuple[bool, str | None]:
        """Look for robots.txt and an XML sitemap. Returns (has_robots_txt, sitemap_url)."""
        parts = urlsplit(site_url)
        origin = f"{parts.scheme}://{parts.netloc}"

        async def get_text(url: str) -> str | None:
            try:
                result = await self.fetch(url, html_only=False)
            except AppError:
                return None
            return result.body[:200_000].decode("utf-8", errors="replace")

        robots = await get_text(f"{origin}/robots.txt")
        has_robots = bool(robots and re.search(r"(?im)^\s*(user-agent|sitemap|disallow|allow)\s*:", robots))

        candidates = []
        if has_robots:
            candidates += re.findall(r"(?im)^\s*sitemap\s*:\s*(\S+)", robots)
        candidates += [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]

        for candidate in list(dict.fromkeys(candidates))[:3]:
            body = await get_text(candidate)
            if body and ("<urlset" in body or "<sitemapindex" in body):
                return has_robots, candidate
        return has_robots, None


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


def parse_document(html: bytes, encoding: str | None, base_url: str) -> ParsedDocument:
    soup = BeautifulSoup(html, "lxml", from_encoding=encoding)

    organization = _extract_json_ld(soup)
    links = _collect_links(soup, base_url)
    social = _social_profiles(links, organization)
    emails, phones = _contacts(soup)
    head = _extract_head(soup)
    audit_counts = _audit_counts(soup)
    nav_items = _nav_items(soup)
    footer_text = _footer_text(soup)
    headings = _headings(soup)  # before stripping - hero H1s often live inside <header>
    js_app = _looks_like_js_app(soup)

    _strip_noise(soup)
    text = _readable_text(soup)

    return ParsedDocument(
        head=head,
        organization=organization,
        links=links,
        nav_items=nav_items,
        footer_text=footer_text,
        headings=headings,
        text=text,
        audit_counts=audit_counts,
        social_profiles=social,
        emails=emails,
        phones=phones,
        looks_like_js_app=js_app,
    )


JS_APP_ROOT = re.compile(r"^(root|app|__next|__nuxt|___gatsby|svelte|q-app)$")


def _looks_like_js_app(soup: BeautifulSoup) -> bool:
    """Typical fingerprints of React / Vue / Next / Nuxt / Angular / Svelte shells."""
    return bool(
        soup.find(id=JS_APP_ROOT)
        or soup.find("app-root")
        or soup.find("script", type="module")
        or soup.find("noscript", string=re.compile(r"javascript", re.I))
    )


def _merge(*lists: list[str]) -> list[str]:
    return list(dict.fromkeys(item for items in lists for item in items))


def _meta(soup: BeautifulSoup, **attrs) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    if tag and tag.get("content"):
        value = " ".join(tag["content"].split())
        return value or None
    return None


def _extract_head(soup: BeautifulSoup) -> dict:
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    html_tag = soup.find("html")
    return {
        "title": " ".join(title.split()) if title else None,
        "description": _meta(soup, name="description") or _meta(soup, property="og:description"),
        "keywords": _meta(soup, name="keywords"),
        "site_name": _meta(soup, property="og:site_name") or _meta(soup, name="application-name"),
        "language": html_tag.get("lang") if html_tag else None,
    }


def _extract_json_ld(soup: BeautifulSoup) -> dict:
    """Pull the organisation / local business entry out of JSON-LD if there is one."""
    wanted = ("Organization", "Corporation", "LocalBusiness", "ProfessionalService", "Store", "WebSite", "Brand")
    keep_keys = ("@type", "name", "legalName", "description", "url", "address", "areaServed", "sameAs", "slogan", "foundingDate")

    candidates = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(isinstance(t, str) and t.endswith(wanted) for t in types):
                candidates.append(item)

    if not candidates:
        return {}
    # Prefer a real organisation over the WebSite entry
    candidates.sort(key=lambda item: str(item.get("@type")) == "WebSite")
    best = candidates[0]
    return {key: best[key] for key in keep_keys if key in best}


def _collect_links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    links = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        links.append((clean, anchor.get_text(" ", strip=True)))
    return links


def _social_profiles(links: list[tuple[str, str]], organization: dict) -> dict[str, str]:
    """Pick the brand's own profile per platform, preferring company pages over posts or people."""
    same_as = organization.get("sameAs") or []
    if isinstance(same_as, str):
        same_as = [same_as]
    # JSON-LD sameAs is the site owner's own declaration, so it wins ties
    urls = [url for url in same_as if isinstance(url, str)] + [url for url, _ in links]

    best: dict[str, tuple[int, str]] = {}
    for url in urls:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
        platform = SOCIAL_DOMAINS.get(host) or next(
            (name for domain, name in SOCIAL_DOMAINS.items() if host.endswith("." + domain)), None
        )
        path = parts.path.strip("/")
        if not platform or not path:
            continue  # not social, or a bare "facebook.com" link
        if any(hint.lower() in url.lower() for hint in SOCIAL_SHARE_HINTS):
            continue
        score = _profile_score(platform, path.lower())
        if score <= 0:
            continue
        if platform not in best or score > best[platform][0]:
            best[platform] = (score, urlunsplit(("https", parts.netloc, parts.path.rstrip("/"), "", "")))
    return {platform: url for platform, (_, url) in best.items()}


POST_PATHS = ("p/", "reel/", "reels/", "tv/", "stories/", "posts/", "status/", "watch", "shorts/", "photo", "video", "pulse/", "feed/", "events/", "hashtag/")


def _profile_score(platform: str, path: str) -> int:
    """0 = not a profile (a post, reel, tweet...), higher = more likely the brand's own page."""
    if any(segment in path + "/" for segment in POST_PATHS):
        return 0
    if platform == "LinkedIn":
        if path.startswith(("company/", "school/", "showcase/")):
            return 3
        if path.startswith("in/"):
            return 1  # a person - usually the founder, keep only as a last resort
        return 0
    if platform == "YouTube":
        return 3 if path.startswith(("@", "channel/", "c/", "user/")) else 0
    return 3 if path.count("/") == 0 else 1


def _contacts(soup: BeautifulSoup) -> tuple[list[str], list[str]]:
    emails, phones = [], []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(email):
                emails.append(email.lower())
        elif href.lower().startswith("tel:"):
            phone = re.sub(r"[^\d+]", "", href[4:])
            if 7 <= len(phone.lstrip("+")) <= 15:
                phones.append(phone)

    footer = soup.find("footer")
    if footer:
        for email in EMAIL_RE.findall(footer.get_text(" ")):
            if not FILE_LIKE_EMAIL.search(email):
                emails.append(email.lower())
    return _merge(emails)[:3], _merge(phones)[:3]


def _pick_extra_pages(links: list[tuple[str, str]], home_url: str, limit: int) -> list[str]:
    if limit <= 0:
        return []
    home_path = urlsplit(home_url).path.rstrip("/")
    scores: dict[str, int] = {}
    for url, text in links:
        if not same_site(url, home_url):
            continue
        path = urlsplit(url).path.lower()
        if path.rstrip("/") == home_path or path.endswith(SKIP_EXTENSIONS):
            continue
        haystack = f"{path} {text.lower()}"
        if any(bad in haystack for bad in LOW_VALUE_PAGES):
            continue
        score = sum(weight for word, weight in PAGE_KEYWORDS.items() if word in haystack)
        if score:
            # shallow pages tend to be the overview pages we actually want
            score -= path.strip("/").count("/")
            scores[url] = max(scores.get(url, 0), score)
    ranked = sorted(scores, key=scores.get, reverse=True)
    return ranked[:limit]


def _nav_items(soup: BeautifulSoup) -> list[str]:
    items = []
    for nav in soup.find_all(["nav", "header"]):
        for anchor in nav.find_all("a"):
            text = anchor.get_text(" ", strip=True)
            if text and 1 < len(text) < 50 and text not in items:
                items.append(text)
    return items


def _footer_text(soup: BeautifulSoup) -> str:
    footer = soup.find("footer")
    if not footer:
        return ""
    text = " ".join(footer.get_text(" ", strip=True).split())
    return text[:600]


def _audit_counts(soup: BeautifulSoup) -> dict:
    images = soup.find_all("img")
    return {
        "images_total": len(images),
        "images_missing_alt": sum(1 for img in images if not (img.get("alt") or "").strip()),
        "has_canonical": soup.find("link", rel=lambda value: value and "canonical" in value) is not None,
        "has_structured_data": soup.find("script", type="application/ld+json") is not None,
        "has_open_graph": soup.find("meta", property=re.compile(r"^og:")) is not None,
        "has_viewport": soup.find("meta", attrs={"name": "viewport"}) is not None,
        "robots": _meta(soup, name="robots"),
    }


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup(NOISE_TAGS + LAYOUT_TAGS):
        tag.decompose()
    # cookie banners / modals are usually just noise for this purpose
    for tag in soup.find_all(attrs={"class": re.compile(r"cookie|consent|gdpr|modal|popup", re.I)}):
        # body often carries classes like "modal-open" - never drop the page itself
        if tag.name not in ("html", "body", "main") and not tag.decomposed:
            tag.decompose()


def _headings(soup: BeautifulSoup) -> dict[str, list[str]]:
    result = {}
    for level in ("h1", "h2", "h3"):
        values = []
        for tag in soup.find_all(level):
            text = " ".join(tag.get_text(" ", strip=True).split())
            if text and text not in values:
                values.append(text[:150])
        result[level] = values
    return result


def _readable_text(soup: BeautifulSoup) -> str:
    root = soup.find("main") or soup.body or soup
    seen, lines = set(), []
    for raw_line in root.get_text("\n").splitlines():
        line = " ".join(raw_line.split())
        if len(line) < 3:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SEO audit (measured, no LLM)
# ---------------------------------------------------------------------------


def _build_seo_audit(
    content: WebsiteContent,
    *,
    static_doc: ParsedDocument,
    rendered_doc: ParsedDocument | None,
    home: FetchResult,
    has_robots: bool,
    sitemap_url: str | None,
) -> SEOAudit:
    # Headings/images: what a visitor (and Google's renderer) finally sees.
    # Meta tags: what is in the raw HTML, which is what most crawlers and social previews read.
    page = rendered_doc or static_doc
    raw = {**page.audit_counts, **{k: static_doc.audit_counts[k] for k in ("has_canonical", "has_open_graph", "robots", "has_viewport")}}
    raw["has_structured_data"] = static_doc.audit_counts["has_structured_data"] or page.audit_counts["has_structured_data"]

    title = content.title or ""
    description = content.meta_description or ""
    h1s = content.headings.get("h1", [])
    word_count = len(content.pages[0].text.split()) if content.pages else 0
    html_size_kb = round(len(home.body) / 1024, 1)
    issues = []

    if not title:
        issues.append("Missing <title> tag.")
    elif len(title) < 30:
        issues.append(f"Title is short ({len(title)} chars); 50-60 characters usually works best.")
    elif len(title) > 60:
        issues.append(f"Title is long ({len(title)} chars) and may be truncated in search results.")

    if not description:
        issues.append("Missing meta description.")
    elif len(description) < 70:
        issues.append(f"Meta description is short ({len(description)} chars); aim for 120-160.")
    elif len(description) > 160:
        issues.append(f"Meta description is long ({len(description)} chars) and may be truncated.")

    if not h1s:
        issues.append("No H1 heading found on the homepage.")
    elif len(h1s) > 1:
        issues.append(f"Homepage has {len(h1s)} H1 headings; one clear H1 is recommended.")

    if raw["images_missing_alt"]:
        issues.append(f"{raw['images_missing_alt']} of {raw['images_total']} images have no alt text.")
    if not raw["has_canonical"]:
        issues.append("No canonical URL tag.")
    if not raw["has_structured_data"]:
        issues.append("No structured data (JSON-LD) found - Organization / LocalBusiness schema is recommended.")
    if not raw["has_open_graph"]:
        issues.append("No Open Graph tags - social shares will not have a proper preview.")
    if not raw["has_viewport"]:
        issues.append("No viewport meta tag - the page may not display well on mobile.")
    if not content.language:
        issues.append("The <html> tag has no lang attribute.")
    if raw["robots"] and "noindex" in raw["robots"].lower():
        issues.append("The page is marked noindex and will not appear in search results.")
    if not has_robots:
        issues.append("No robots.txt file found.")
    if not sitemap_url:
        issues.append("No XML sitemap found (checked robots.txt, /sitemap.xml and /sitemap_index.xml).")
    if word_count < 250:
        issues.append(f"Homepage has thin text content (~{word_count} words).")
    if rendered_doc is not None:
        issues.append(
            "Main content only appears after JavaScript runs. Crawlers that don't execute JavaScript and "
            "social link previews see an almost empty page - server-side rendering or pre-rendering is recommended."
        )
    if home.elapsed_ms > 2500:
        issues.append(f"Homepage HTML took {home.elapsed_ms} ms to download; aim for under 1 second.")
    if html_size_kb > 500:
        issues.append(f"Homepage HTML is large ({html_size_kb} KB).")

    uses_https = content.final_url.startswith("https://")
    if not uses_https:
        issues.append("The site is not served over HTTPS.")

    return SEOAudit(
        current_title=content.title,
        title_length=len(title),
        current_meta_description=content.meta_description,
        meta_description_length=len(description),
        h1_count=len(h1s),
        h1_headings=h1s[:5],
        images_total=raw["images_total"],
        images_missing_alt=raw["images_missing_alt"],
        has_canonical=raw["has_canonical"],
        has_structured_data=raw["has_structured_data"],
        has_open_graph=raw["has_open_graph"],
        has_viewport_meta=raw["has_viewport"],
        has_robots_txt=has_robots,
        sitemap_url=sitemap_url,
        language=content.language,
        word_count=word_count,
        uses_https=uses_https,
        rendered_with_javascript=rendered_doc is not None,
        response_time_ms=home.elapsed_ms,
        html_size_kb=html_size_kb,
        issues=issues,
    )
