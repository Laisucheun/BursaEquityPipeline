from __future__ import annotations

import asyncio

from bursa.scrapers.robots import RobotsCache


def run(coro):
    return asyncio.run(coro)


def fetch_returning(status: int, text: str = ""):
    calls: list[str] = []

    async def fetch(url: str) -> tuple[int, str]:
        calls.append(url)
        return status, text

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_disallowed_path_is_denied() -> None:
    cache = RobotsCache("TestBot/1.0")
    fetch = fetch_returning(200, "User-agent: *\nDisallow: /private/\n")

    assert run(cache.is_allowed("https://example.com/private/x", fetch)) is False
    assert run(cache.is_allowed("https://example.com/public/x", fetch)) is True


def test_allow_all_permits_everything() -> None:
    cache = RobotsCache("TestBot/1.0")
    fetch = fetch_returning(200, "User-agent: *\nAllow: /\n")

    assert run(cache.is_allowed("https://example.com/anything", fetch)) is True


def test_missing_robots_txt_means_allow_all() -> None:
    cache = RobotsCache("TestBot/1.0")
    fetch = fetch_returning(404)

    assert run(cache.is_allowed("https://example.com/anything", fetch)) is True


def test_unreachable_robots_txt_fails_open() -> None:
    """A network error fetching robots.txt must not block the whole run."""
    cache = RobotsCache("TestBot/1.0")

    async def fetch(url: str) -> tuple[int, str]:
        raise TimeoutError("no route to host")

    assert run(cache.is_allowed("https://example.com/anything", fetch)) is True


def test_server_error_fetching_robots_txt_fails_open() -> None:
    cache = RobotsCache("TestBot/1.0")
    fetch = fetch_returning(503)

    assert run(cache.is_allowed("https://example.com/anything", fetch)) is True


def test_robots_txt_is_fetched_once_per_host() -> None:
    cache = RobotsCache("TestBot/1.0")
    fetch = fetch_returning(200, "User-agent: *\nAllow: /\n")

    async def three_calls() -> None:
        await cache.is_allowed("https://example.com/a", fetch)
        await cache.is_allowed("https://example.com/b", fetch)
        await cache.is_allowed("https://example.com/c", fetch)

    run(three_calls())
    assert len(fetch.calls) == 1  # type: ignore[attr-defined]


def test_different_hosts_are_cached_independently() -> None:
    cache = RobotsCache("TestBot/1.0")
    allow_fetch = fetch_returning(200, "User-agent: *\nAllow: /\n")
    deny_fetch = fetch_returning(200, "User-agent: *\nDisallow: /\n")

    assert run(cache.is_allowed("https://allowed.example.com/x", allow_fetch)) is True
    assert run(cache.is_allowed("https://blocked.example.com/x", deny_fetch)) is False


def test_crawl_delay_is_read_from_the_cached_policy() -> None:
    cache = RobotsCache("TestBot/1.0")
    fetch = fetch_returning(200, "User-agent: *\nCrawl-delay: 5\nAllow: /\n")

    run(cache.is_allowed("https://example.com/x", fetch))
    assert cache.crawl_delay("https://example.com/x") == 5.0


def test_crawl_delay_is_none_when_undeclared() -> None:
    cache = RobotsCache("TestBot/1.0")
    fetch = fetch_returning(200, "User-agent: *\nAllow: /\n")

    run(cache.is_allowed("https://example.com/x", fetch))
    assert cache.crawl_delay("https://example.com/x") is None
