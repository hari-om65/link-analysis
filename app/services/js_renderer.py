"""Headless Chromium fallback for sites that build their content with JavaScript.

A React / Vue / Angular site usually ships an almost empty HTML shell
(`<div id="root"></div>`), so plain HTTP fetching sees nothing. When that
happens the extractor asks this renderer to open the page in a real browser
and hand back the final DOM.

Safety: every request the browser makes (scripts, XHR, iframes, redirects) goes
through the same SSRF check as normal fetching, so a page cannot use the
browser to reach internal addresses. Images, fonts and media are blocked to
keep it fast.

Playwright is optional - if it isn't installed or Chromium can't start, the
renderer just reports itself as unavailable and the API keeps working.
"""

import asyncio
import logging
from urllib.parse import urlsplit

from app.config import Settings
from app.exceptions import AppError, WebsiteFetchError, WebsiteTimeoutError
from app.utils.validators import ensure_public_url

logger = logging.getLogger(__name__)

BLOCKED_RESOURCES = {"image", "media", "font"}


class JSRenderer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright = None
        self._browser = None
        self._start_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max(settings.js_render_max_concurrent, 1))
        self._broken = False

        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            logger.info("Playwright not installed - JavaScript rendering disabled")
            self._broken = True

    @property
    def available(self) -> bool:
        return not self._broken

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        async with self._start_lock:
            if self._browser is not None:
                return self._browser
            from playwright.async_api import async_playwright

            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    args=["--disable-dev-shm-usage", "--no-sandbox"]
                )
                logger.info("Headless Chromium started for JavaScript rendering")
            except NotImplementedError:
                # Windows + `uvicorn --reload` uses a selector event loop that can't spawn processes
                self._broken = True
                logger.warning("JavaScript rendering unavailable on this event loop (run uvicorn without --reload)")
                raise WebsiteFetchError("JavaScript rendering is not available on this server.") from None
            except Exception as exc:
                self._broken = True
                logger.warning("Could not start Chromium (%s) - run `playwright install chromium`", exc)
                raise WebsiteFetchError("JavaScript rendering is not available on this server.") from None
        return self._browser

    async def render(self, url: str) -> tuple[str, str]:
        """Return (final_url, html) after the page's JavaScript has run."""
        if self._broken:
            raise WebsiteFetchError("JavaScript rendering is not available on this server.")

        url = await ensure_public_url(url)
        browser = await self._ensure_browser()
        timeout_ms = int(self.settings.js_render_timeout_seconds * 1000)
        checked_hosts: dict[str, bool] = {}

        async with self._slots:
            context = await browser.new_context(
                user_agent=self.settings.user_agent,
                viewport={"width": 1366, "height": 900},
                service_workers="block",
                accept_downloads=False,
            )
            try:
                page = await context.new_page()
                await page.route("**/*", lambda route: self._guard(route, checked_hosts))
                from playwright.async_api import TimeoutError as PlaywrightTimeout

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeout:
                    raise WebsiteTimeoutError("The website took too long to render.") from None
                except AppError:
                    raise
                except Exception as exc:
                    raise WebsiteFetchError(f"The browser could not open the page ({exc.__class__.__name__}).") from None

                # Give client-side frameworks a moment to fetch data and paint
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
                except PlaywrightTimeout:
                    pass

                final_url = page.url
                await ensure_public_url(final_url)
                return final_url, await page.content()
            finally:
                await context.close()

    async def _guard(self, route, checked_hosts: dict[str, bool]) -> None:
        request = route.request
        if request.resource_type in BLOCKED_RESOURCES:
            await route.abort()
            return

        url = request.url
        if url.startswith(("data:", "blob:")):
            await route.continue_()
            return

        host = (urlsplit(url).hostname or "").lower()
        if host not in checked_hosts:
            checked_hosts[host] = await self.is_allowed(url)
        if checked_hosts[host]:
            await route.continue_()
        else:
            logger.info("Blocked browser request to non-public address: %s", url[:120])
            await route.abort()

    @staticmethod
    async def is_allowed(url: str) -> bool:
        try:
            await ensure_public_url(url)
            return True
        except AppError:
            return False

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None
