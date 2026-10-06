"""Per-host robots.txt compliance.

At the scale of "every company in the watchlist" the scraper touches dozens of
distinct hosts, each of which gets exactly one policy decision cached for the
rest of the run - no per-site manual check, no override. A host that disallows
the target path is skipped, never crawled anyway.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

log = logging.getLogger(__name__)


class RobotsCache:
    """Fetches and caches one ``robots.txt`` per host for the life of a run."""

    def __init__(self, user_agent: str, fetch_timeout: float = 10.0) -> None:
        self.user_agent = user_agent
        self.fetch_timeout = fetch_timeout
        self._parsers: dict[str, RobotFileParser | None] = {}

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme or "https", parts.netloc, "", "", ""))

    async def _get_parser(self, origin: str, fetch) -> RobotFileParser | None:  # type: ignore[no-untyped-def]
        if origin in self._parsers:
            return self._parsers[origin]

        parser = RobotFileParser()
        robots_url = f"{origin}/robots.txt"
        try:
            status, text = await fetch(robots_url)
        except Exception as exc:
            log.warning("could not fetch %s (%s); treating as allow-all", robots_url, exc)
            self._parsers[origin] = None
            return None

        if status == 404:
            # No robots.txt at all conventionally means "everything is allowed".
            self._parsers[origin] = None
            return None
        if status >= 400:
            log.warning(
                "robots.txt at %s returned %s; treating as allow-all", robots_url, status
            )
            self._parsers[origin] = None
            return None

        parser.parse(text.splitlines())
        self._parsers[origin] = parser
        return parser

    async def is_allowed(self, url: str, fetch) -> bool:  # type: ignore[no-untyped-def]
        """Whether ``url`` may be fetched under the cached policy for its host.

        ``fetch`` is an ``async (robots_url: str) -> (status_code: int, text:
        str)`` callable, injected so this cache doesn't own an HTTP client
        itself - the caller's client (with its own retry/timeout policy)
        does the actual fetching.
        """
        origin = self._origin(url)
        parser = await self._get_parser(origin, fetch)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        """The site's own ``Crawl-delay``, if it declared one, else ``None``."""
        origin = self._origin(url)
        parser = self._parsers.get(origin)
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None
