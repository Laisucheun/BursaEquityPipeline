"""Orchestration: sniff each company's IR site, ingest what's found, log the
outcome either way.

Companies are processed one at a time, deliberately. ``PoliteHttpClient``
supports concurrent fetches across different hosts (tested in
``test_http_client.py``), but a plain SQLAlchemy ``Session`` is not safe to
write to from interleaved concurrent coroutines - and at watchlist scale
(dozens of companies, not thousands) the wall-clock cost of staying serial is
minutes, not hours. If this ever needs to run over a much larger company list,
the fix is to run the network-bound sniffing concurrently and collect results
before doing any serial database writes - not to make the writes concurrent.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from bursa.config import get_settings
from bursa.db.enums import DocSource, DocType, ScrapeStatus
from bursa.db.models import Company, Document, ScrapeAttempt
from bursa.pipeline.ingest import ingest_file
from bursa.scrapers.content_filter import has_financial_statements
from bursa.scrapers.http_client import PoliteHttpClient, RobotsDisallowed, looks_like_pdf
from bursa.scrapers.ir_fallback import PdfCandidate, sniff_annual_report

log = logging.getLogger(__name__)


def _log_attempt(
    session: Session,
    company: Company,
    status: ScrapeStatus,
    run_label: str,
    detail: str | None = None,
    document_id: int | None = None,
) -> ScrapeAttempt:
    attempt = ScrapeAttempt(
        company_id=company.id,
        source=DocSource.IR,
        status=status,
        document_id=document_id,
        detail=detail[:1000] if detail else None,
        run_label=run_label,
    )
    session.add(attempt)
    session.flush()
    return attempt


async def scrape_company(
    session: Session,
    client: PoliteHttpClient,
    company: Company,
    run_label: str,
    dry_run: bool = False,
    years: int = 1,
    pdf_dir: Path | None = None,
) -> list[ScrapeAttempt]:
    """Sniff and (unless ``dry_run``) ingest one company's annual report(s).

    ``years`` caps how many of the most recent distinct years found among
    the site's own candidates to fetch - 1 (the default) is the original
    "newest only" behaviour. It also drives how deep `sniff_annual_report`
    is willing to crawl: that function's own hop limit grows at runtime
    (configurable via the `scraper_base_hops`/`scraper_hop_escalation_step`/
    `scraper_max_hops_ceiling`/`scraper_max_pages_per_sniff` settings) when
    fewer distinct years turn up than ``years`` asks for, rather than
    silently handing back whatever the first few hops happened to find.

    Content-verified during the crawl itself (`sniff_annual_report`'s
    `verify_content`), not just afterwards by this function's own
    `_scrape_one_year` - turned on here whenever this isn't a ``dry_run``.
    Without that, a decoy merely dated the same year as a real,
    not-yet-visited page can satisfy "found enough years" before the real
    page is ever reached (confirmed real on AMMB). Dry runs keep their
    existing never-downloads-anything contract, so this stays off for them
    - a dry run can still under-report on a site where this exact
    coincidence would otherwise bite, only a real run actually corrects it.

    Always returns at least one logged attempt - never raises. A company
    with fewer distinct years available than asked for just gets fewer
    attempts back, not a padded failure."""
    if not company.ir_homepage_url:
        return [
            _log_attempt(
                session, company, ScrapeStatus.SKIPPED_NO_URL, run_label,
                detail="Company.ir_homepage_url is not set",
            )
        ]

    settings = get_settings()
    try:
        result = await sniff_annual_report(
            client,
            company.ir_homepage_url,
            min_years=years,
            base_hops=settings.scraper_base_hops,
            hop_escalation_step=settings.scraper_hop_escalation_step,
            max_hops_ceiling=settings.scraper_max_hops_ceiling,
            max_pages=settings.scraper_max_pages_per_sniff,
            verify_content=not dry_run,
        )
    except Exception as exc:
        log.exception("sniffing %s (%s) failed", company.stock_code, company.ir_homepage_url)
        return [_log_attempt(session, company, ScrapeStatus.ERROR, run_label, detail=str(exc))]

    target_years = sorted({c.year for c in result.candidates if c.year is not None}, reverse=True)[:years]
    if not target_years:
        # No year detected anywhere - fall back to the single first candidate
        # found (parts_for_best_year()'s own fallback) rather than guessing.
        fallback = result.parts_for_best_year()
        if not fallback:
            if result.homepage_blocked_by_robots:
                return [
                    _log_attempt(
                        session, company, ScrapeStatus.SKIPPED_ROBOTS, run_label,
                        detail=f"robots.txt disallows {company.ir_homepage_url}",
                    )
                ]
            return [
                _log_attempt(
                    session, company, ScrapeStatus.NOT_FOUND, run_label,
                    detail=f"visited {len(result.pages_visited)} page(s), no matching PDF found",
                )
            ]
        return [
            await _scrape_one_year(session, client, company, run_label, None, fallback, dry_run, pdf_dir=pdf_dir)
        ]

    return [
        await _scrape_one_year(
            session, client, company, run_label, year,
            [c for c in result.candidates if c.year == year], dry_run, pdf_dir=pdf_dir,
        )
        for year in target_years
    ]


async def _scrape_one_year(
    session: Session,
    client: PoliteHttpClient,
    company: Company,
    run_label: str,
    year: int | None,
    parts: list[PdfCandidate],
    dry_run: bool,
    pdf_dir: Path | None = None,
) -> ScrapeAttempt:
    """Download, content-check, and (unless ``dry_run``) ingest one year's
    worth of same-year candidates for one company. Factored out of
    `scrape_company` so fetching several years just calls this once per
    year - the per-candidate download/content-check/ingest logic itself
    doesn't change based on how many years are being fetched."""
    if dry_run:
        detail = "; ".join(f"{p.url} (year={p.year})" for p in parts)
        return _log_attempt(
            session, company, ScrapeStatus.FOUND, run_label, detail=f"[dry-run] {detail}"
        )

    # Pass 1: download every same-year candidate and keep only the ones whose
    # own content actually has a financial statement - link text is no
    # longer trusted for this at all (see ir_fallback's module docstring).
    # A temp file that fails the check is deleted immediately and never
    # ingested; "discard" and "never stored" are the same action here.
    kept: list[tuple[Path, PdfCandidate]] = []
    discarded: list[str] = []
    errors: list[str] = []

    for part in parts:
        # Determine destination path
        if pdf_dir:
            company_dir = pdf_dir / company.stock_code
            company_dir.mkdir(parents=True, exist_ok=True)
            raw_name = part.url.rsplit("/", 1)[-1]
            if "?" in raw_name:
                from urllib.parse import parse_qs, urlsplit as _us
                qs = parse_qs(_us(part.url).query)
                raw_name = qs.get("sFileName", [raw_name.split("?")[0]])[0]
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", raw_name)[:120] or f"{uuid4().hex[:8]}.pdf"
            if not safe_name.lower().endswith(".pdf"):
                safe_name += ".pdf"
            tmp_path = company_dir / safe_name
        else:
            tmp_path = Path(tempfile.mktemp(suffix=".pdf"))

        try:
            if pdf_dir:
                # Stream directly to disk — avoids holding full PDF in memory
                status_code, _ = await client.download_to_file(part.url, tmp_path)
                if not (200 <= status_code < 300):
                    errors.append(f"{part.url}: HTTP {status_code}")
                    tmp_path.unlink(missing_ok=True)
                    continue
                # Check magic bytes from file
                with open(tmp_path, "rb") as f:
                    if not looks_like_pdf(f.read(5)):
                        errors.append(f"{part.url}: response is not a PDF")
                        tmp_path.unlink(missing_ok=True)
                        continue
            else:
                fetched = await client.get(part.url)
                if not fetched.ok:
                    errors.append(f"{part.url}: HTTP {fetched.status_code}")
                    continue
                if not looks_like_pdf(fetched.content):
                    errors.append(f"{part.url}: response is not a PDF")
                    continue
                tmp_path.write_bytes(fetched.content)
        except RobotsDisallowed:
            errors.append(f"{part.url}: robots.txt disallows this path")
            tmp_path.unlink(missing_ok=True)
            continue
        except Exception as exc:
            errors.append(f"{part.url}: {exc}")
            tmp_path.unlink(missing_ok=True)
            continue

        if has_financial_statements(tmp_path):
            kept.append((tmp_path, part))
        else:
            discarded.append(part.url)
            tmp_path.unlink(missing_ok=True)

    if not kept:
        if discarded and not errors:
            return _log_attempt(
                session, company, ScrapeStatus.NOT_FOUND, run_label,
                detail=(
                    f"downloaded {len(discarded)} candidate(s) for year={year}, "
                    f"none contained a financial statement: {'; '.join(discarded)}"
                ),
            )
        return _log_attempt(
            session, company, ScrapeStatus.ERROR, run_label,
            detail="; ".join(errors) or "no part could be downloaded",
        )

    # Pass 2: ingest only the survivors. Grouped only when more than one
    # survives - grouping in the decoys pass 1 already discarded would
    # misrepresent them as parts of the same filing.
    group_key = f"{company.stock_code}-{year}" if len(kept) > 1 else None
    kept_documents: list[Document] = []
    try:
        for tmp_path, part in kept:
            document, _created = ingest_file(
                session,
                tmp_path,
                source=DocSource.IR,
                company_id=company.id,
                source_url=part.url,
                doc_type=DocType.ANNUAL_REPORT,
                document_group_key=group_key,
            )
            kept_documents.append(document)
    finally:
        if not pdf_dir:
            for tmp_path, _part in kept:
                tmp_path.unlink(missing_ok=True)

    detail = f"kept {len(kept_documents)}/{len(parts)} candidate(s), year={year}"
    if discarded:
        detail += f"; discarded (no financial statements): {'; '.join(discarded)}"
    if errors:
        detail += f"; {len(errors)} part(s) failed: {'; '.join(errors)}"

    return _log_attempt(
        session, company, ScrapeStatus.FOUND, run_label,
        detail=detail,
        document_id=kept_documents[-1].id,
    )


async def scrape_annual_reports(
    session: Session,
    companies: list[Company],
    dry_run: bool = False,
    run_label: str | None = None,
    years: int = 1,
    pdf_dir: Path | None = None,
) -> list[ScrapeAttempt]:
    """Scrape every given company's annual report(s) - the ``years`` most
    recent distinct years found on each site. See module docstring for why
    this is serial across companies."""
    settings = get_settings()
    run_label = run_label or f"run-{uuid4().hex[:8]}"

    attempts: list[ScrapeAttempt] = []
    async with PoliteHttpClient(
        user_agent=settings.scraper_user_agent,
        min_delay_seconds=settings.scraper_min_delay_seconds,
        max_concurrency=settings.scraper_max_concurrency,
        max_retries=settings.scraper_max_retries,
        timeout_seconds=settings.scraper_timeout_seconds,
    ) as client:
        for company in companies:
            company_attempts = await scrape_company(
                session, client, company, run_label, dry_run=dry_run, years=years, pdf_dir=pdf_dir,
            )
            attempts.extend(company_attempts)
            for attempt in company_attempts:
                log.info("%s (%s): %s", company.stock_code, company.name, attempt.status)

    return attempts
