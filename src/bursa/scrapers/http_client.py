"""A polite async HTTP client: per-host rate limiting, robots.txt compliance,
bounded global concurrency, and retry with backoff.

This is the only thing in ``scrapers/`` that talks to the network. Everything
else (the sniffer, the orchestrator) calls through it, so politeness is
enforced in one place rather than by convention at every call site.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from bursa.scrapers.robots import RobotsCache

log = logging.getLogger(__name__)

# Retried: transient network/server trouble. Not retried: 4xx other than 429 -
# those mean the request itself is wrong, and retrying won't fix it.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    status_code: int
    content: bytes
    headers: httpx.Headers
    final_url: str  # after redirects

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def text(self, encoding: str = "utf-8") -> str:
        return self.content.decode(encoding, errors="replace")


class RobotsDisallowed(RuntimeError):
    """The target host's robots.txt disallows this path. Never overridden."""


class _HostThrottle:
    """Serialises requests to one host with a minimum delay between them."""

    def __init__(self, min_delay: float) -> None:
        self.min_delay = min_delay
        self._lock = asyncio.Lock()
        self._last_request: float = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last_request
            remaining = self.min_delay - elapsed
            if remaining > 0:
                # A little jitter so a batch of hosts doesn't fall into lockstep.
                await asyncio.sleep(remaining + random.uniform(0, 0.25))
            self._last_request = time.monotonic()


class PoliteHttpClient:
    """Rate-limited, retrying, robots.txt-respecting HTTP client.

    One instance per scrape run. Hosts are throttled independently, so the
    long tail of distinct IR domains doesn't wait on each other, while any
    single host never gets hit faster than ``min_delay_seconds``.
    """

    def __init__(
        self,
        user_agent: str,
        min_delay_seconds: float = 1.0,
        max_concurrency: int = 8,
        max_retries: int = 3,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max_retries
        self._robots = RobotsCache(user_agent)
        self._throttles: dict[str, _HostThrottle] = {}
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            follow_redirects=True,
            # Tests inject a MockTransport here to avoid any real network I/O.
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> PoliteHttpClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    @staticmethod
    def _host(url: str) -> str:
        return urlsplit(url).netloc

    def _throttle_for(self, url: str) -> _HostThrottle:
        host = self._host(url)
        if host not in self._throttles:
            self._throttles[host] = _HostThrottle(self.min_delay_seconds)
        return self._throttles[host]

    async def _raw_get(self, url: str) -> tuple[int, str]:
        """Unthrottled, unretried GET used only by the robots.txt cache itself
        - it must not recurse into the policy check it's establishing."""
        response = await self._client.get(url)
        return response.status_code, response.text

    async def get(self, url: str) -> FetchResult:
        """A polite, policy-checked GET. Raises ``RobotsDisallowed`` if the
        host's robots.txt says no - the caller must treat that as a skip, not
        retry or route around it."""
        if not await self._robots.is_allowed(url, self._raw_get):
            raise RobotsDisallowed(url)

        delay = self._robots.crawl_delay(url)
        if delay and delay > self.min_delay_seconds:
            # The site asked for more room than our default - honour it.
            self._throttles[self._host(url)] = _HostThrottle(delay)

        async with self._semaphore:
            throttle = self._throttle_for(url)
            await throttle.wait_turn()
            return await self._get_with_retries(url)

    async def _get_with_retries(self, url: str) -> FetchResult:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.get(url)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt > self.max_retries:
                    raise
                await self._backoff(attempt, None)
                log.warning("attempt %s/%s for %s failed: %s", attempt, self.max_retries, url, exc)
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt <= self.max_retries:
                retry_after = _parse_retry_after(response.headers.get("retry-after"))
                await self._backoff(attempt, retry_after)
                log.warning(
                    "attempt %s/%s for %s got %s, retrying",
                    attempt,
                    self.max_retries,
                    url,
                    response.status_code,
                )
                continue

            return FetchResult(
                url=url,
                status_code=response.status_code,
                content=response.content,
                headers=response.headers,
                final_url=str(response.url),
            )

    @staticmethod
    async def _backoff(attempt: int, retry_after: float | None) -> None:
        if retry_after is not None:
            await asyncio.sleep(retry_after)
            return
        # Exponential backoff with jitter: 1s, 2s, 4s, ...
        await asyncio.sleep(min(30.0, 2 ** (attempt - 1)) + random.uniform(0, 0.5))


    async def download_to_file(self, url: str, dest: Path) -> tuple[int, str]:
        """Stream a URL directly to *dest*, returning (status_code, final_url).

        Avoids holding the full response body in memory — critical for
        large PDFs that would otherwise OOM the process."""
        if not await self._robots.is_allowed(url, self._raw_get):
            raise RobotsDisallowed(url)

        delay = self._robots.crawl_delay(url)
        if delay and delay > self.min_delay_seconds:
            self._throttles[self._host(url)] = _HostThrottle(delay)

        async with self._semaphore:
            throttle = self._throttle_for(url)
            await throttle.wait_turn()

            attempt = 0
            while True:
                attempt += 1
                try:
                    async with self._client.stream("GET", url) as response:
                        if response.status_code in _RETRYABLE_STATUS and attempt <= self.max_retries:
                            retry_after = _parse_retry_after(response.headers.get("retry-after"))
                            await self._backoff(attempt, retry_after)
                            continue

                        with open(dest, "wb") as f:
                            async for chunk in response.aiter_bytes(chunk_size=65536):
                                f.write(chunk)

                        return response.status_code, str(response.url)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt > self.max_retries:
                        raise
                    await self._backoff(attempt, None)
                    log.warning("attempt %s/%s for %s failed: %s", attempt, self.max_retries, url, exc)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # An HTTP-date form; not worth parsing for this volume.


def looks_like_pdf(content: bytes) -> bool:
    """Magic-byte check. A redirect to a login/error page is common and must
    not silently pass through as a real document."""
    return content[:5] == b"%PDF-"
