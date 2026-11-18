"""Post-processing for the competitor section.

Competitor names come from the LLM, so they are only suggestions. To make them
a bit more trustworthy we:
  * drop entries that are actually the analysed brand itself,
  * normalise the website field,
  * check that each suggested competitor website really exists and answers
    over HTTP (`website_verified`). This does not prove they are competitors,
    only that the company / domain is real and not hallucinated.
"""

import asyncio
import logging

import httpx

from app.exceptions import AppError
from app.schemas import Competitor, CompetitorAnalysis
from app.utils.validators import ensure_public_url, same_site

logger = logging.getLogger(__name__)

RESEARCH_NOTE = (
    "Competitors are AI-generated suggestions based on the website's category, services and market. "
    "They are not taken from a market-research database. 'website_verified' only confirms the "
    "suggested domain exists and responds; please review before making business decisions."
)


class CompetitorService:
    def __init__(self, client: httpx.AsyncClient, verify_sites: bool = True, timeout: float = 6.0):
        self.client = client
        self.verify_sites = verify_sites
        self.timeout = timeout

    async def enrich(self, analysis: CompetitorAnalysis, brand_url: str, brand_name: str) -> CompetitorAnalysis:
        competitors = [
            c for c in analysis.competitors if not self._is_same_brand(c, brand_url, brand_name)
        ]

        for competitor in competitors:
            competitor.website = self._clean_website(competitor.website)

        if self.verify_sites:
            await asyncio.gather(*(self._verify(c) for c in competitors))

        analysis.competitors = competitors
        analysis.research_note = RESEARCH_NOTE
        return analysis

    @staticmethod
    def _is_same_brand(competitor: Competitor, brand_url: str, brand_name: str) -> bool:
        if competitor.website and "." in competitor.website:
            url = competitor.website if "://" in competitor.website else f"https://{competitor.website}"
            if same_site(url, brand_url):
                return True
        return competitor.name.strip().lower() == brand_name.strip().lower()

    @staticmethod
    def _clean_website(website: str | None) -> str | None:
        if not website or website.lower() in ("null", "none", "n/a", "unknown"):
            return None
        website = website.strip()
        if "://" not in website:
            website = f"https://{website}"
        return website

    async def _verify(self, competitor: Competitor) -> None:
        if not competitor.website:
            competitor.website_verified = None
            return
        try:
            url = await ensure_public_url(competitor.website)
            # Redirects are not followed on purpose: a 3xx already proves the site exists,
            # and following it could lead to an internal address we have not checked.
            request = self.client.build_request("GET", url, timeout=self.timeout)
            response = await self.client.send(request, stream=True, follow_redirects=False)
            await response.aclose()
            # Some sites answer bots with 403 but they clearly exist
            competitor.website_verified = response.status_code < 400 or response.status_code in (401, 403, 429)
        except (AppError, httpx.HTTPError) as exc:
            logger.info("Competitor site check failed for %s: %s", competitor.website, exc)
            competitor.website_verified = False
