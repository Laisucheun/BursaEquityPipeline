from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bursa.db.enums import DocSource, DocType, ScrapeStatus
from bursa.db.models import Company, Document
from bursa.scrapers import ir_fallback as ir_fallback_mod
from bursa.scrapers import run_annual_reports as run_mod
from bursa.scrapers.http_client import PoliteHttpClient
from bursa.scrapers.run_annual_reports import scrape_annual_reports, scrape_company
from bursa.storage.local import LocalBlobStore

FIXTURES = Path(__file__).parent / "fixtures" / "ir_html"


def minimal_pdf(marker: bytes = b"") -> bytes:
    """A tiny but real PDF, distinguishable by ``marker`` so two different
    filings never collide on sha256 the way two identical fixtures would."""
    return b"%PDF-1.4\n1 0 obj<<>>endobj\n%% " + marker + b"\ntrailer<<>>\n%%EOF"


MINIMAL_PDF = minimal_pdf(b"default")


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBlobStore:
    blob_store = LocalBlobStore(tmp_path / "storage")
    # ingest_file() resolves the store via bursa.pipeline.ingest.get_store.
    from bursa.pipeline import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "get_store", lambda: blob_store)
    return blob_store


@pytest.fixture(autouse=True)
def _assume_financial_statements(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file exercises provenance/grouping/dedup, not the content filter
    itself (see test_content_filter.py). ``MINIMAL_PDF``/``minimal_pdf()``
    are tiny synthetic PDFs with no real statement content - without this,
    every download here would be (correctly) discarded by the real content
    check, and every ``FOUND`` assertion below would fail for an unrelated
    reason. A test that wants to exercise the discard path itself
    (`test_a_candidate_with_no_financial_statements_is_discarded_not_ingested`)
    overrides this back with its own `monkeypatch.setattr` call.

    Patched in *two* places, not one: `run_annual_reports._scrape_one_year`'s
    own ingest-time check, and `ir_fallback._verify_candidates`'s crawl-time
    one (`sniff_annual_report`'s `verify_content`, on whenever `scrape_company`
    isn't a dry run) - each module did `from content_filter import
    has_financial_statements`, which copies the name into each importing
    module's own namespace at import time, so patching one doesn't reach the
    other. Missing this second patch left 8 tests in this file failing with
    NOT_FOUND instead of FOUND the first time `verify_content` was wired in -
    the real content check correctly rejected every `MINIMAL_PDF` *during the
    crawl itself*, before `_scrape_one_year`'s own (patched) check ever got a
    chance to run at all."""
    monkeypatch.setattr(run_mod, "has_financial_statements", lambda path: True)
    monkeypatch.setattr(ir_fallback_mod, "has_financial_statements", lambda path: True)


def flat_hub_client(pages: dict[str, str] | None = None, pdf_paths: set[str] | None = None):
    pages = pages or {
        "/": load("flat_hub_home.html"),
        "/media-investors/annual-reports": load("flat_hub_reports.html"),
    }
    pdf_paths = pdf_paths or {"/assets/annual_report/EXPWR_IAR_2024.pdf"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path in pdf_paths:
            return httpx.Response(
                200, content=MINIMAL_PDF, headers={"content-type": "application/pdf"}
            )
        if path in pages:
            return httpx.Response(200, text=pages[path])
        return httpx.Response(404)

    return PoliteHttpClient(
        user_agent="TestBot/1.0", min_delay_seconds=0.0, transport=httpx.MockTransport(handler)
    )


def megamenu_client():
    pages = {
        "/": load("megamenu_home.html"),
        "/investor-relations/annual-reports/": load("megamenu_reports.html"),
    }
    # Distinct content per part - two parts of one real filing are never
    # byte-identical, and ingest_file's sha256 dedupe would otherwise
    # (correctly, for real distinct files) collapse them to one document.
    pdfs = {
        "/media/3ydmzqiw/ar-2025-part1.pdf": minimal_pdf(b"part1"),
        "/media/9zks81aa/ar-2025-part2.pdf": minimal_pdf(b"part2"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path in pdfs:
            return httpx.Response(
                200, content=pdfs[path], headers={"content-type": "application/pdf"}
            )
        if path in pages:
            return httpx.Response(200, text=pages[path])
        return httpx.Response(404)

    return PoliteHttpClient(
        user_agent="TestBot/1.0", min_delay_seconds=0.0, transport=httpx.MockTransport(handler)
    )


def add_company(session: Session, ir_url: str | None, stock_code: str = "9999") -> Company:
    company = Company(stock_code=stock_code, name="Example Power Berhad", ir_homepage_url=ir_url)
    session.add(company)
    session.flush()
    return company


# --------------------------------------------------------------------------


def test_company_without_ir_url_is_skipped_not_silently_ignored(session: Session) -> None:
    company = add_company(session, ir_url=None)

    async def go():
        async with flat_hub_client() as client:
            return (await scrape_company(session, client, company, "run-1"))[0]

    attempt = run(go())
    assert attempt.status == ScrapeStatus.SKIPPED_NO_URL
    assert attempt.company_id == company.id


def test_a_found_report_is_ingested_with_correct_provenance(
    session: Session, store: LocalBlobStore
) -> None:
    company = add_company(session, ir_url="https://example-power.test/")

    async def go():
        async with flat_hub_client() as client:
            return (await scrape_company(session, client, company, "run-1"))[0]

    attempt = run(go())

    assert attempt.status == ScrapeStatus.FOUND
    assert attempt.document_id is not None

    document = session.get(Document, attempt.document_id)
    assert document.company_id == company.id
    assert document.source == DocSource.IR
    assert document.doc_type == DocType.ANNUAL_REPORT
    assert document.source_url == "https://example-power.test/assets/annual_report/EXPWR_IAR_2024.pdf"
    assert document.document_group_key is None  # single-part filing


def test_multi_part_filing_shares_one_group_key_across_two_documents(
    session: Session, store: LocalBlobStore
) -> None:
    company = add_company(session, ir_url="https://example-bank.test/", stock_code="1295")

    async def go():
        async with megamenu_client() as client:
            return (await scrape_company(session, client, company, "run-1"))[0]

    attempt = run(go())
    assert attempt.status == ScrapeStatus.FOUND

    documents = session.scalars(
        select(Document).where(Document.company_id == company.id)
    ).all()
    assert len(documents) == 2
    group_keys = {d.document_group_key for d in documents}
    assert group_keys == {"1295-2025"}
    assert {d.source_url for d in documents} == {
        "https://example-bank.test/media/3ydmzqiw/ar-2025-part1.pdf",
        "https://example-bank.test/media/9zks81aa/ar-2025-part2.pdf",
    }


def test_no_candidates_found_is_logged_not_found(session: Session) -> None:
    company = add_company(session, ir_url="https://example-retail.test/")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/":
            return httpx.Response(200, text=load("no_reports_home.html"))
        return httpx.Response(404)

    async def go():
        async with PoliteHttpClient(
            "TestBot/1.0", min_delay_seconds=0.0, transport=httpx.MockTransport(handler)
        ) as client:
            return (await scrape_company(session, client, company, "run-1"))[0]

    attempt = run(go())
    assert attempt.status == ScrapeStatus.NOT_FOUND
    assert attempt.document_id is None


def test_robots_disallowed_is_logged_and_never_downloads(session: Session) -> None:
    company = add_company(session, ir_url="https://example-power.test/")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        pytest.fail(f"must not fetch {request.url} when robots.txt disallows /")

    async def go():
        async with PoliteHttpClient(
            "TestBot/1.0", min_delay_seconds=0.0, transport=httpx.MockTransport(handler)
        ) as client:
            return (await scrape_company(session, client, company, "run-1"))[0]

    attempt = run(go())
    assert attempt.status == ScrapeStatus.SKIPPED_ROBOTS


def test_dry_run_resolves_but_never_downloads_or_ingests(session: Session) -> None:
    company = add_company(session, ir_url="https://example-power.test/")
    download_attempted = {"flag": False}

    pages = {
        "/": load("flat_hub_home.html"),
        "/media-investors/annual-reports": load("flat_hub_reports.html"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path.endswith(".pdf"):
            download_attempted["flag"] = True
            return httpx.Response(200, content=MINIMAL_PDF)
        if request.url.path in pages:
            return httpx.Response(200, text=pages[request.url.path])
        return httpx.Response(404)

    async def go():
        async with PoliteHttpClient(
            "TestBot/1.0", min_delay_seconds=0.0, transport=httpx.MockTransport(handler)
        ) as client:
            return (await scrape_company(session, client, company, "run-1", dry_run=True))[0]

    attempt = run(go())
    assert attempt.status == ScrapeStatus.FOUND
    assert "[dry-run]" in attempt.detail
    assert attempt.document_id is None
    assert download_attempted["flag"] is False
    assert session.scalars(select(Document).where(Document.company_id == company.id)).all() == []


def test_rerunning_the_same_company_does_not_duplicate_the_document(
    session: Session, store: LocalBlobStore
) -> None:
    company = add_company(session, ir_url="https://example-power.test/")

    async def go():
        async with flat_hub_client() as client:
            first = (await scrape_company(session, client, company, "run-1"))[0]
        async with flat_hub_client() as client:
            second = (await scrape_company(session, client, company, "run-2"))[0]
        return first, second

    first, second = run(go())

    assert first.status == second.status == ScrapeStatus.FOUND
    assert first.document_id == second.document_id  # sha256 dedupe in ingest_file

    documents = session.scalars(
        select(Document).where(Document.company_id == company.id)
    ).all()
    assert len(documents) == 1


def test_a_candidate_with_no_financial_statements_is_discarded_not_ingested(
    session: Session, store: LocalBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the broadened link-collection strategy in
    ir_fallback.py: a downloaded candidate that content_filter says has no
    real statement content must never be ingested, even though it passed
    every earlier check (robots, HTTP 200, looks-like-pdf)."""
    monkeypatch.setattr(run_mod, "has_financial_statements", lambda path: False)
    company = add_company(session, ir_url="https://example-power.test/")

    async def go():
        async with flat_hub_client() as client:
            return (await scrape_company(session, client, company, "run-1"))[0]

    attempt = run(go())

    assert attempt.status == ScrapeStatus.NOT_FOUND
    assert attempt.document_id is None
    assert "financial statement" in attempt.detail
    assert session.scalars(select(Document).where(Document.company_id == company.id)).all() == []


def test_only_candidates_with_financial_statements_are_kept_from_a_mixed_batch(
    session: Session, store: LocalBlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public Bank's real shape: several candidates share one year's listing
    (the real filing plus decoys like a chairman's statement), and only the
    ones with real statement content should survive - grouped together, the
    discarded ones left out of the group entirely."""
    real_url = "https://example-bank.test/media/3ydmzqiw/ar-2025-part1.pdf"

    def fake_check(path):
        # ingest_file has already run by the time this is called in the real
        # flow, but here we key off which part is being checked via a
        # side-channel: patch at the source_url level isn't available, so
        # approximate by content instead (part1 vs part2 differ in bytes).
        return b"part1" in path.read_bytes()

    monkeypatch.setattr(run_mod, "has_financial_statements", fake_check)
    company = add_company(session, ir_url="https://example-bank.test/", stock_code="1295")

    async def go():
        async with megamenu_client() as client:
            return (await scrape_company(session, client, company, "run-1"))[0]

    attempt = run(go())
    assert attempt.status == ScrapeStatus.FOUND
    assert "discarded" in attempt.detail

    documents = session.scalars(
        select(Document).where(Document.company_id == company.id)
    ).all()
    assert len(documents) == 1
    assert documents[0].source_url == real_url
    assert documents[0].document_group_key is None  # only one survivor - no group


def test_years_parameter_fetches_several_distinct_years(
    session: Session, store: LocalBlobStore
) -> None:
    company = add_company(session, ir_url="https://example-power.test/")
    pdf_content = {
        "/assets/annual_report/EXPWR_IAR_2024.pdf": minimal_pdf(b"2024"),
        "/assets/annual_report/EXPWR_IAR_2023.pdf": minimal_pdf(b"2023"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path in pdf_content:
            return httpx.Response(200, content=pdf_content[path])
        if path == "/":
            return httpx.Response(200, text=load("flat_hub_home.html"))
        if path == "/media-investors/annual-reports":
            return httpx.Response(200, text=load("flat_hub_reports.html"))
        return httpx.Response(404)

    async def go():
        async with PoliteHttpClient(
            "TestBot/1.0", min_delay_seconds=0.0, transport=httpx.MockTransport(handler)
        ) as client:
            return await scrape_company(session, client, company, "run-1", years=2)

    attempts = run(go())

    assert [a.status for a in attempts] == [ScrapeStatus.FOUND, ScrapeStatus.FOUND]
    assert "year=2024" in attempts[0].detail
    assert "year=2023" in attempts[1].detail

    documents = session.scalars(select(Document).where(Document.company_id == company.id)).all()
    assert {d.source_url for d in documents} == {
        "https://example-power.test/assets/annual_report/EXPWR_IAR_2024.pdf",
        "https://example-power.test/assets/annual_report/EXPWR_IAR_2023.pdf",
    }


def test_years_parameter_is_capped_by_what_the_site_actually_has(
    session: Session, store: LocalBlobStore
) -> None:
    """flat_hub_reports.html only has 2024/2023/2022 - asking for more years
    than exist must yield fewer attempts, not a padded failure."""
    company = add_company(session, ir_url="https://example-power.test/")
    pdf_paths = {
        "/assets/annual_report/EXPWR_IAR_2024.pdf",
        "/assets/annual_report/EXPWR_IAR_2023.pdf",
        "/assets/annual_report/EXPWR_IAR_2022.pdf",
    }

    async def go():
        async with flat_hub_client(pdf_paths=pdf_paths) as client:
            return await scrape_company(session, client, company, "run-1", years=10)

    attempts = run(go())
    assert len(attempts) == 3
    assert all(a.status == ScrapeStatus.FOUND for a in attempts)


def test_scrape_annual_reports_runs_a_whole_list_and_labels_every_attempt(
    session: Session, monkeypatch: pytest.MonkeyPatch, store: LocalBlobStore
) -> None:
    good = add_company(session, ir_url="https://example-power.test/", stock_code="1111")
    skipped = add_company(session, ir_url=None, stock_code="2222")

    # scrape_annual_reports builds its own PoliteHttpClient from settings; swap
    # it for one backed by the mock transport instead of touching real network.
    def fake_client_factory(*args, **kwargs):
        return flat_hub_client()

    monkeypatch.setattr(run_mod, "PoliteHttpClient", fake_client_factory)

    attempts = run(scrape_annual_reports(session, [good, skipped]))

    assert len(attempts) == 2
    assert {a.run_label for a in attempts} == {attempts[0].run_label}  # one shared label
    statuses = {a.company_id: a.status for a in attempts}
    assert statuses[good.id] == ScrapeStatus.FOUND
    assert statuses[skipped.id] == ScrapeStatus.SKIPPED_NO_URL
