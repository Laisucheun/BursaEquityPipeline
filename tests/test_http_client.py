from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from bursa.scrapers.http_client import PoliteHttpClient, RobotsDisallowed, looks_like_pdf


def run(coro):
    return asyncio.run(coro)


ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"
ROBOTS_DENY_ALL = "User-agent: *\nDisallow: /\n"


def make_client(handler, **kwargs) -> PoliteHttpClient:
    transport = httpx.MockTransport(handler)
    return PoliteHttpClient(
        user_agent="TestBot/1.0",
        min_delay_seconds=kwargs.pop("min_delay_seconds", 0.0),
        max_retries=kwargs.pop("max_retries", 2),
        transport=transport,
        **kwargs,
    )


def test_a_normal_fetch_succeeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(200, content=b"hello")

    async def go() -> None:
        async with make_client(handler) as client:
            result = await client.get("https://example.com/page")
            assert result.ok
            assert result.content == b"hello"

    run(go())


def test_robots_disallowed_path_raises_and_never_fetches_it() -> None:
    fetched_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched_paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_DENY_ALL)
        return httpx.Response(200, content=b"should never be reached")

    async def go() -> None:
        async with make_client(handler) as client:
            with pytest.raises(RobotsDisallowed):
                await client.get("https://example.com/private/page")

    run(go())
    assert "/private/page" not in fetched_paths


def test_transient_5xx_is_retried_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    async def go() -> None:
        async with make_client(handler, max_retries=3) as client:
            result = await client.get("https://example.com/flaky")
            assert result.ok
            assert result.content == b"ok"

    run(go())
    assert attempts["n"] == 3


def test_retries_are_exhausted_and_the_error_status_is_returned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(503)

    async def go() -> None:
        async with make_client(handler, max_retries=1) as client:
            result = await client.get("https://example.com/always-down")
            assert result.status_code == 503

    run(go())


def test_a_plain_404_is_not_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        attempts["n"] += 1
        return httpx.Response(404)

    async def go() -> None:
        async with make_client(handler, max_retries=3) as client:
            result = await client.get("https://example.com/missing")
            assert result.status_code == 404

    run(go())
    assert attempts["n"] == 1  # no retries wasted on a request that is simply wrong


def test_requests_to_the_same_host_are_throttled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(200, content=b"x")

    async def go() -> float:
        async with make_client(handler, min_delay_seconds=0.2) as client:
            start = time.monotonic()
            await client.get("https://example.com/a")
            await client.get("https://example.com/b")
            return time.monotonic() - start

    elapsed = run(go())
    assert elapsed >= 0.2


def test_different_hosts_are_not_throttled_against_each_other() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(200, content=b"x")

    async def go() -> float:
        async with make_client(handler, min_delay_seconds=1.0) as client:
            start = time.monotonic()
            await asyncio.gather(
                client.get("https://a.example.com/x"),
                client.get("https://b.example.com/x"),
            )
            return time.monotonic() - start

    elapsed = run(go())
    # Two different hosts, each with its own 1s throttle, run concurrently -
    # this must take much less than the 2s a naive shared throttle would need.
    assert elapsed < 1.0


def test_looks_like_pdf() -> None:
    assert looks_like_pdf(b"%PDF-1.7\n...") is True
    assert looks_like_pdf(b"<html>not a pdf</html>") is False
    assert looks_like_pdf(b"") is False
