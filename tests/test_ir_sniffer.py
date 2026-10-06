from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from bursa.scrapers import ir_fallback as ir_fallback_mod
from bursa.scrapers.http_client import PoliteHttpClient
from bursa.scrapers.ir_fallback import (
    EXCLUDE_PATTERN,
    _classify_link,
    _extract_year,
    find_nav_candidates,
    find_pdf_candidates,
    sniff_annual_report,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ir_html"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Unit-level: pure parsing logic against fixture content
# --------------------------------------------------------------------------


def test_flat_hub_link_text_alone_is_enough() -> None:
    """TNB-shaped page: the link's own text says "Annual Report for 2024"."""
    html = load("flat_hub_reports.html")
    candidates = find_pdf_candidates(html, "https://example-power.test/media-investors/annual-reports")

    years = {c.year for c in candidates}
    assert years == {2022, 2023, 2024}
    newest = max(candidates, key=lambda c: c.year)
    assert newest.url.endswith("EXPWR_IAR_2024.pdf")


def test_megamenu_hub_needs_ancestor_context() -> None:
    """Public Bank-shaped page (matching the real live DOM, confirmed by
    direct inspection): a row carries no "annual report" text of its own at
    all - the label is a `<div>` sibling *before* the whole list, not a
    wrapping ancestor - and a year's report shares that one list with its
    AGM notice, administrative details, and proxy form."""
    html = load("megamenu_reports.html")
    candidates = find_pdf_candidates(html, "https://example-bank.test/investor-relations/annual-reports/")

    urls = {c.url for c in candidates}
    # Every row that ALSO carries no distinguishing text of its own (chairman
    # statement, MD review, sustainability/governance reports) is swept in
    # too via the same label fallback - a known, accepted trade-off: several
    # extra harmless PDFs from the same filing bundle beat missing the real
    # report entirely, which is what happened before this fix existed.
    assert urls == {
        "https://example-bank.test/media/3ydmzqiw/ar-2025-part1.pdf",
        "https://example-bank.test/media/9zks81aa/ar-2025-part2.pdf",
        "https://example-bank.test/media/1p5h3yqu/chairman-2025.pdf",
        "https://example-bank.test/media/2q6i4zrv/md-review-2025.pdf",
        "https://example-bank.test/media/ff66aa77/sustainability-2025.pdf",
        "https://example-bank.test/media/gg77bb88/cg-report-2025.pdf",
    }
    assert all(c.year == 2025 for c in candidates)


def test_notice_and_proxy_pdfs_are_excluded_even_though_pdf_text_is_generic() -> None:
    html = load("megamenu_reports.html")
    candidates = find_pdf_candidates(html, "https://example-bank.test/investor-relations/annual-reports/")
    urls = {c.url for c in candidates}

    assert not any("notice-agm" in u for u in urls)
    assert not any("proxy" in u for u in urls)


def test_a_block_mentioning_both_annual_report_and_notice_is_excluded() -> None:
    """Exclude wins on a tie - the conservative choice for a mixed bundle."""
    html = load("megamenu_reports.html")
    candidates = find_pdf_candidates(html, "https://example-bank.test/investor-relations/annual-reports/")
    urls = {c.url for c in candidates}

    assert not any("bundle-2025" in u for u in urls)


def test_a_distinctly_titled_financial_statements_pdf_is_no_longer_missed() -> None:
    """Real gap found on two live sites (CIMB, AMMB): each PDF gets its own
    distinct heading, with no shared "2025 Annual Report" label to fall back
    on the way Public Bank's page has. The genuine financial-statements
    volume's own heading never says "annual report" - only the narrative
    volume's does - so the old match-required classifier returned just the
    narrative one and missed the real filing. Link collection is now wide
    open specifically so this no longer happens; telling the two apart by
    content is content_filter.has_financial_statements's job, not this
    module's - see both docstrings."""
    html = load("distinct_headings_reports.html")
    candidates = find_pdf_candidates(html, "https://example-conglomerate.test/annual-reports")
    urls = {c.url for c in candidates}

    assert "https://example-conglomerate.test/docs/2025-integrated-annual-report.pdf" in urls
    assert "https://example-conglomerate.test/docs/2025-financial-statements.pdf" in urls
    assert not any("notice-agm" in u for u in urls)
    assert not any("proxy" in u for u in urls)
    assert all(c.year == 2025 for c in candidates)


def test_percent_encoded_day_of_month_does_not_forge_a_spurious_year() -> None:
    """Real bug found on the live CIMB site: a filename encoding "FYE 31
    December 2019" as "FYE%2031%20December%202019" contains the literal
    substring "2031" once percent-encoded ("%20" immediately before "31"
    supplies its leading "20") - a higher, entirely spurious year that
    would have silently become the site-wide "most recent year" and made
    `parts_for_best_year()` return only that one 2019 filing, discarding
    every real, current-year candidate. Confirmed against the real URL."""
    url = (
        "https://www.cimb.com/content/dam/cimb/group/documents/investor-relations/"
        "annual-reports/2019/CIMB%20Corporate%20Governance%20Report%20FYE%2031%20December%202019.pdf"
    )
    assert _extract_year("Corporate Governance Report", url) == 2019


def test_quarterly_announcements_sharing_the_annual_year_folder_are_excluded() -> None:
    """Real gap found on the live PETRONAS Chemicals site: quarterly Bursa
    announcements, analyst decks and call transcripts sit in the same
    year-dated folder as the real annual financial report and genuinely
    contain real financial statements (just quarterly ones) - the
    post-download content check alone would keep them, so link text is the
    only signal left to exclude this specific decoy class."""
    html = load("quarterly_alongside_annual.html")
    candidates = find_pdf_candidates(html, "https://example-petrochem.test/investor-relations")
    urls = {c.url for c in candidates}

    assert "https://example-petrochem.test/docs/2026/ir2025.pdf" in urls
    assert "https://example-petrochem.test/docs/2026/fr2025.pdf" in urls
    assert not any("2q2026" in u for u in urls)


def test_generic_governance_and_policy_documents_are_excluded() -> None:
    """Real decoys found scraping a batch of 48 companies: several sites list
    dozens of generic corporate-policy PDFs right alongside the real annual
    report (confirmed real: Sarawak Oil Palms and Genting Plantations each
    yielded 24 candidates this way, all policy documents, zero real
    filings). The content check downstream would eventually reject all of
    these too, but excluding them before download avoids real, observed
    waste (two dozen PDFs fetched and content-checked per company for
    nothing)."""
    real_decoy_filenames = [
        "Revised-Whistle-Blowing-Policy-and-Procedure-Version-1.2-26Aug2026.pdf",
        "Group-Audit-Committee-TOR.pdf",
        "Board-Charter-12.8.2026.pdf",
        "Code-of-Business-Conduct-and-Ethics-12.8.2026.pdf",
        "Supplier-Code-of-Conduct.pdf",
        "BKB-PDPA-Notice.pdf",
        "BKB-EGM-Min-5-Aug-2026.pdf",
        "POL-HOPL-03V2.0_Anti-Bribery-and-Corruption-System-Policy.pdf",
        "THMC-PDPA-Rev-6.pdf",
        "FOOD-SAFETY-POLICY-FARM-FRESH.pdf",
        "Personal-Data-Protection-Policy.pdf",
        "EKOVEST-privacy-statement.pdf",
        "Deposit-Insurance-System-Brochures.pdf",
        "prospectus.pdf",
    ]
    for name in real_decoy_filenames:
        assert EXCLUDE_PATTERN.search(name), f"expected {name!r} to be excluded"

    # A real annual report / financial statements filename must never
    # collide with any of these phrasings.
    real_keepers = ["cimb-fr-2025.pdf", "Integrated-Annual-Report-2025.pdf", "ar2026_full-report.pdf"]
    for name in real_keepers:
        assert not EXCLUDE_PATTERN.search(name), f"expected {name!r} to survive"


def test_investor_conference_decks_are_excluded() -> None:
    """Real decoys found testing the scraper against prior years on Public
    Bank's site: investor-roadshow/conference decks sit in the same dated
    media folder as the real annual report and genuinely contain enough
    tables/figures to pass the downstream content check - only link
    text/filename exclusion catches them."""
    real_decoy_filenames = [
        "cgsi-3rd-regional-financial-conference-3-4dec.pdf",
        "pbb_jpm-asean-financials-forum-2-3oct24.pdf",
        "ubs-oneasean-summit-4-5mar24_26feb.pdf",
        "pbb_nomura-asean-conference-2024-15-16jan24.pdf",
        "pbb_asean-banks-tour-11jun24.pdf",
        "pbb_invest-malaysia-2016-presentation.pdf",
        "pbb_ubs-malaysia-corporate-day-2019-19-20-march.pdf",
        "pbb_spotlight-on-malaysia-2016.pdf",
        "maybank-ibg-s-invest-asean-2026-7-8jul-sg.pdf",
    ]
    for name in real_decoy_filenames:
        assert EXCLUDE_PATTERN.search(name), f"expected {name!r} to be excluded"

    real_keepers = ["TNB_IAR_2024.pdf", "vitrox_ar2024.pdf"]
    for name in real_keepers:
        assert not EXCLUDE_PATTERN.search(name), f"expected {name!r} to survive"


def test_no_reports_present_yields_nothing() -> None:
    html = load("no_reports_home.html")
    assert find_pdf_candidates(html, "https://example-retail.test/") == []


def test_nav_candidates_prefer_annual_report_over_generic_investor_relations() -> None:
    html = load("megamenu_home.html")
    candidates = find_nav_candidates(html, "https://example-bank.test/")

    assert candidates[0] == "https://example-bank.test/investor-relations/annual-reports/"


def test_nav_candidates_ignore_hash_and_off_site_links() -> None:
    html = load("megamenu_home.html")
    candidates = find_nav_candidates(html, "https://example-bank.test/")

    assert all(url.startswith("https://example-bank.test/") for url in candidates)


def test_flat_hub_nav_matches_media_and_investors_label() -> None:
    """The label varies across real sites ("MEDIA & INVESTORS" vs "Investor
    Relations") - the pattern must not assume one exact phrase."""
    html = load("flat_hub_home.html")
    candidates = find_nav_candidates(html, "https://example-power.test/")

    assert "https://example-power.test/media-investors/annual-reports" in candidates


def test_classify_link_resolves_via_the_preceding_heading_fallback() -> None:
    """Regression guard for two real bugs found against live pages, not just
    the synthetic fixture:

    1. Sibling-conflation: a fixed-depth blob would blend a nearby "Notice of
       AGM" row into a real report row once the climb reached their shared
       ancestor. Level-by-level checking must not.
    2. The real Public Bank page's row (`Part 1 - Business Review`) carries no
       "annual report" text at any ancestor level - only a heading *before*
       the list does, which a pure ancestor climb can never see. The
       preceding-heading fallback is what actually resolves this one; without
       it, this same assertion would fail with `is_report is False`.
    """
    from bs4 import BeautifulSoup

    html = load("megamenu_reports.html")
    soup = BeautifulSoup(html, "lxml")
    part1_link = soup.find("a", href=lambda h: h and "part1" in h)

    is_report, matched = _classify_link(part1_link)
    assert is_report is True
    assert "notice" not in matched.lower()
    assert "proxy" not in matched.lower()


def test_classify_link_exclusion_still_wins_even_with_the_heading_fallback() -> None:
    """The Notice of AGM row must stay excluded even though it now falls
    under a page that also contains a real "2025 Annual Report" heading
    elsewhere - exclusion at the row's own level runs before the fallback
    ever gets a chance to misapply an unrelated heading."""
    from bs4 import BeautifulSoup

    html = load("megamenu_reports.html")
    soup = BeautifulSoup(html, "lxml")
    notice_link = soup.find("a", href=lambda h: h and "notice-agm" in h)

    is_report, _ = _classify_link(notice_link)
    assert is_report is False


# --------------------------------------------------------------------------
# Integration-level: the full crawl over a mocked multi-page site
# --------------------------------------------------------------------------


def make_site(pages: dict[str, str]) -> PoliteHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path in pages:
            return httpx.Response(200, text=pages[path], headers={"content-type": "text/html"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    return PoliteHttpClient(user_agent="TestBot/1.0", min_delay_seconds=0.0, transport=transport)


def test_flat_hub_resolves_in_one_hop() -> None:
    site = {
        "/": load("flat_hub_home.html"),
        "/media-investors/annual-reports": load("flat_hub_reports.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(client, "https://example-power.test/")

    result = run(go())
    assert result.best_year == 2024
    parts = result.parts_for_best_year()
    assert len(parts) == 1
    assert parts[0].url.endswith("2024.pdf")
    assert len(result.pages_visited) == 2  # home, then the hub - stopped once found


def test_megamenu_site_resolves_in_two_hops_with_both_parts() -> None:
    site = {
        "/": load("megamenu_home.html"),
        "/investor-relations/annual-reports/": load("megamenu_reports.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(client, "https://example-bank.test/")

    result = run(go())
    assert result.best_year == 2025
    parts = result.parts_for_best_year()
    urls = {p.url for p in parts}
    # The real two parts must be present - that's the point of the fix.
    assert "https://example-bank.test/media/3ydmzqiw/ar-2025-part1.pdf" in urls
    assert "https://example-bank.test/media/9zks81aa/ar-2025-part2.pdf" in urls
    # Every candidate genuinely shares the bundle's year - see the accepted
    # trade-off note in test_megamenu_hub_needs_ancestor_context.
    assert all("agm" not in u and "proxy" not in u and "bundle" not in u for u in urls)


def test_a_stray_homepage_pdf_does_not_stop_the_crawl_before_the_real_hub() -> None:
    """Real regression found on two live sites (CIMB, AMMB) right after link
    collection was broadened: the raw homepage itself links to an unrelated
    PDF (a privacy notice, a coffee-table book) that now satisfies "found
    candidates" under the wide-open classifier, stopping the crawl on the
    homepage before it ever followed the nav link to the real reports hub.
    The fix requires at least one nav hop before an early stop is trusted -
    this proves the crawl still reaches the real hub instead of settling for
    the stray PDF."""
    site = {
        "/": load("stray_pdf_home.html"),
        "/investor-relations": load("distinct_headings_reports.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(client, "https://example-conglomerate.test/")

    result = run(go())
    # The stray homepage PDF is harmlessly accumulated (it has no detectable
    # year, so parts_for_best_year() - what scrape_company actually uses -
    # never selects it); what matters is that the crawl didn't stop on the
    # homepage and never reached the real hub at all.
    assert len(result.pages_visited) == 2  # home, then the real hub
    urls = {c.url for c in result.parts_for_best_year()}
    assert urls == {"https://example-conglomerate.test/docs/2025-financial-statements.pdf",
                     "https://example-conglomerate.test/docs/2025-integrated-annual-report.pdf"}


def test_site_with_no_annual_report_yields_no_candidates() -> None:
    site = {"/": load("no_reports_home.html")}

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(client, "https://example-retail.test/")

    result = run(go())
    assert result.candidates == []


def test_a_dead_hop_is_skipped_without_aborting_the_crawl() -> None:
    """The nav points somewhere that 404s; the crawl must not raise."""
    site = {"/": load("megamenu_home.html")}  # /investor-relations/... 404s

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(client, "https://example-bank.test/")

    result = run(go())
    assert result.candidates == []


# --------------------------------------------------------------------------
# Dynamic hop-limit escalation: real need confirmed on AMMB/Public Bank,
# whose own nav puts older years' reports behind more hops than a "newest
# only" crawl ever follows - see ir_fallback.sniff_annual_report's docstring.
# --------------------------------------------------------------------------


def test_hop_limit_escalates_to_reach_years_behind_a_one_year_per_hop_archive() -> None:
    """5 distinct years, one per hop, each only reachable via the previous
    page's own "Past Annual Reports" link - a shape a flat MAX_HOPS=3 budget
    cannot reach past year 3 of. Asking for 5 years must still find all 5,
    not silently settle for however many the base hop budget happened to
    reach."""
    site = {
        "/": load("deep_archive_home.html"),
        "/investors/annual-reports": load("deep_archive_hub.html"),
        "/investors/annual-reports/archive/2024": load("deep_archive_1.html"),
        "/investors/annual-reports/archive/2022": load("deep_archive_2.html"),
        "/investors/annual-reports/archive/2020": load("deep_archive_3.html"),
        "/investors/annual-reports/archive/2018": load("deep_archive_4.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(
                client, "https://example-bank.test/", min_years=5, base_hops=3
            )

    result = run(go())
    years = {c.year for c in result.candidates}
    assert years == {2026, 2024, 2022, 2020, 2018}
    # home + hub + 4 archive pages - every page needed to reach the 5th year,
    # not more than that once it's found.
    assert len(result.pages_visited) == 6


def test_hop_limit_never_escalates_when_the_base_budget_already_has_enough() -> None:
    """Same site, asking for only as many years as the base budget already
    reaches unescalated - must not crawl deeper than it needs to."""
    site = {
        "/": load("deep_archive_home.html"),
        "/investors/annual-reports": load("deep_archive_hub.html"),
        "/investors/annual-reports/archive/2024": load("deep_archive_1.html"),
        "/investors/annual-reports/archive/2022": load("deep_archive_2.html"),
        "/investors/annual-reports/archive/2020": load("deep_archive_3.html"),
        "/investors/annual-reports/archive/2018": load("deep_archive_4.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(
                client, "https://example-bank.test/", min_years=1, base_hops=3
            )

    result = run(go())
    assert result.best_year == 2026
    assert len(result.pages_visited) == 2  # home, then the hub - stopped once found


def test_hop_limit_escalation_is_proportional_to_the_shortfall_not_a_flat_step() -> None:
    """Needing 5 years against 1 already found is a shortfall of 4 - bigger
    than the (deliberately tiny, here) escalation step of 1 - so the budget
    must jump by the shortfall, not creep up one step at a time and have to
    re-escalate on every subsequent hop."""
    site = {
        "/": load("deep_archive_home.html"),
        "/investors/annual-reports": load("deep_archive_hub.html"),
        "/investors/annual-reports/archive/2024": load("deep_archive_1.html"),
        "/investors/annual-reports/archive/2022": load("deep_archive_2.html"),
        "/investors/annual-reports/archive/2020": load("deep_archive_3.html"),
        "/investors/annual-reports/archive/2018": load("deep_archive_4.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(
                client,
                "https://example-bank.test/",
                min_years=5,
                base_hops=1,
                hop_escalation_step=1,
                max_hops_ceiling=20,
            )

    result = run(go())
    years = {c.year for c in result.candidates}
    assert years == {2026, 2024, 2022, 2020, 2018}


def test_hop_limit_never_escalates_past_the_ceiling_even_when_still_short() -> None:
    """A target this site can never satisfy (only 5 years exist) must still
    terminate - via the ceiling, not by hanging - once the frontier runs out
    or the ceiling is hit, whichever comes first."""
    site = {
        "/": load("deep_archive_home.html"),
        "/investors/annual-reports": load("deep_archive_hub.html"),
        "/investors/annual-reports/archive/2024": load("deep_archive_1.html"),
        "/investors/annual-reports/archive/2022": load("deep_archive_2.html"),
        "/investors/annual-reports/archive/2020": load("deep_archive_3.html"),
        "/investors/annual-reports/archive/2018": load("deep_archive_4.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(
                client,
                "https://example-bank.test/",
                min_years=100,
                base_hops=1,
                max_hops_ceiling=4,
            )

    result = run(go())
    # Capped at the ceiling (base_hops=1 escalating only up to max_hops_ceiling=4):
    # home + hub + 3 archive hops = 5 pages, never reaching the 4th archive page.
    assert len(result.pages_visited) == 5
    years = {c.year for c in result.candidates}
    assert years == {2026, 2024, 2022, 2020}


def test_an_ever_growing_nav_never_causes_an_infinite_crawl() -> None:
    """The real adversarial case `max_pages` exists for: a site whose nav
    keeps handing back a fresh, never-before-seen URL at every single hop
    (no cycle for the `visited` set to catch) and never supplies enough
    distinct years to satisfy an unreachable target. Without an independent
    page-count cap, hop-limit escalation alone would let this run forever."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<html><body><a href="/archive/0">Annual Report Archive</a></body></html>',
            )
        if request.url.path.startswith("/archive/"):
            n = int(request.url.path.rsplit("/", 1)[-1])
            # Always the same single year - this target can never be met no
            # matter how deep the crawl goes - and always a brand new URL.
            return httpx.Response(
                200,
                text=(
                    '<html><body><a href="/report/2020.pdf">Annual Report 2020</a>'
                    f'<a href="/archive/{n + 1}">Annual Report Archive</a></body></html>'
                ),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)

    async def go():
        async with PoliteHttpClient(
            user_agent="TestBot/1.0", min_delay_seconds=0.0, transport=transport
        ) as client:
            return await sniff_annual_report(
                client,
                "https://example-infinite.test/",
                min_years=5,  # unreachable - every archive page repeats year 2020
                base_hops=3,
                hop_escalation_step=3,
                max_hops_ceiling=50,
                max_pages=12,
            )

    result = run(go())
    assert len(result.pages_visited) <= 12
    assert {c.year for c in result.candidates} == {2020}


# --------------------------------------------------------------------------
# "Select Year" dropdown recovery: real need confirmed live on Public Bank's
# own site - its current-year page links to *nothing* else in static HTML
# (no nav hop, however deep, would ever find an older year), but carries a
# fully server-rendered <select name="year"> naming every year back to 2004,
# each <option>'s own value a real, independently-fetchable static page.
# --------------------------------------------------------------------------


def test_year_dropdown_options_are_followed_instead_of_only_nav_links() -> None:
    site = {
        "/": load("year_dropdown_home.html"),
        "/investor-relations/annual-reports/": load("year_dropdown_hub.html"),
        "/investor-relations/annual-reports/2025-annual-report/": load("year_dropdown_2025.html"),
        "/investor-relations/annual-reports/2024-annual-report/": load("year_dropdown_2024.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(
                client, "https://example-bank.test/", min_years=3, base_hops=3
            )

    result = run(go())
    years = {c.year for c in result.candidates}
    assert years == {2026, 2025, 2024}
    # home + hub + the 2 older-year pages actually needed - never the 2023
    # option too, since 3 years was already enough.
    assert len(result.pages_visited) == 4
    assert not any("2023" in url for url in result.pages_visited)


def test_year_dropdown_fetches_as_many_older_years_as_still_needed() -> None:
    """Same site, asking for one more year than the previous test - the 2023
    option (present in the dropdown but not previously needed) must now get
    followed too, proving this scales with the actual shortfall rather than
    always stopping at some fixed count."""
    site = {
        "/": load("year_dropdown_home.html"),
        "/investor-relations/annual-reports/": load("year_dropdown_hub.html"),
        "/investor-relations/annual-reports/2025-annual-report/": load("year_dropdown_2025.html"),
        "/investor-relations/annual-reports/2024-annual-report/": load("year_dropdown_2024.html"),
        "/investor-relations/annual-reports/2023-annual-report/": load("year_dropdown_2023.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(
                client, "https://example-bank.test/", min_years=4, base_hops=3
            )

    result = run(go())
    years = {c.year for c in result.candidates}
    assert years == {2026, 2025, 2024, 2023}


def test_find_year_nav_candidates_ignores_a_label_that_merely_mentions_a_year() -> None:
    """A label that merely *mentions* a year ("FY2024 (Restated)") is not this
    pattern - only an element whose text is nothing but the year itself
    counts, so an unrelated form (a language picker, say) can never misfire
    into being treated as a year switcher."""
    html = (
        '<select name="lang"><option value="/en/">English (est. 2024)</option>'
        '<option value="/bm/">Bahasa</option></select>'
        '<a href="/press/fy2024-restated-notice">FY2024 (restated)</a>'
    )
    from bursa.scrapers.ir_fallback import find_year_nav_candidates

    assert find_year_nav_candidates(html, "https://example-bank.test/") == []


def test_find_year_nav_candidates_follows_a_tab_bar_labelled_with_fy_years() -> None:
    """AMMB's real shape: a sliding tab bar whose tabs are ordinary <a href>
    links labelled "FY2025" etc - never matched by `find_nav_candidates`
    (that text matches none of NAV_PATTERN's "investor"/"annual report"
    phrasings), plus its own "Archive" tab bundling every older year the
    individually-labelled tabs don't bother listing."""
    html = (
        '<div class="tab__list">'
        '<a class="tab" href="/investor-relations/annual-report/fy2025">FY2025</a>'
        '<a class="tab" href="/investor-relations/annual-report/fy2024">FY2024</a>'
        '<a class="tab" href="/investor-relations/annual-report/archive">Archive</a>'
        "</div>"
    )
    from bursa.scrapers.ir_fallback import find_year_nav_candidates

    result = find_year_nav_candidates(html, "https://example-ammb.test/investor-relations/annual-report")
    assert result == [
        (2025, "https://example-ammb.test/investor-relations/annual-report/fy2025"),
        (2024, "https://example-ammb.test/investor-relations/annual-report/fy2024"),
        (None, "https://example-ammb.test/investor-relations/annual-report/archive"),
    ]


def test_sniff_follows_a_year_tab_bar_and_its_archive_tab_end_to_end() -> None:
    """Full crawl over the AMMB-shaped site: the hub's own tab bar is
    followed for the years still missing, and its "Archive" tab - which
    bundles several older years in one page rather than naming just one -
    gets queued too once the individually-labelled tabs alone aren't
    enough."""
    site = {
        "/": load("year_tabs_home.html"),
        "/investor-relations/annual-report": load("year_tabs_hub.html"),
        "/investor-relations/annual-report/fy2025": load("year_tabs_fy2025.html"),
        "/investor-relations/annual-report/fy2024": load("year_tabs_fy2024.html"),
        "/investor-relations/annual-report/archive": load("year_tabs_archive.html"),
    }

    async def go():
        async with make_site(site) as client:
            return await sniff_annual_report(
                client, "https://example-ammb.test/", min_years=4, base_hops=3
            )

    result = run(go())
    years = {c.year for c in result.candidates}
    # 2026 (the hub's own current-year PDF) + 2025 + 2024 from the
    # individually-labelled tabs, plus 2011/2010 bundled in the one
    # "Archive" tab needed to reach 4+ distinct years.
    assert years == {2026, 2025, 2024, 2011, 2010}


# --------------------------------------------------------------------------
# verify_content: the fix for a real gap found live-verifying the above - a
# decoy merely dated the same year as a real, not-yet-visited page used to
# satisfy "found enough distinct years" before the real page was ever
# reached (confirmed real on AMMB: a homepage coffee-table-book PDF
# coincidentally dated 2025 stopped the crawl one year short of its real
# fy2025 tab).
# --------------------------------------------------------------------------


def _pdf_bytes(marker: bytes) -> bytes:
    """A real `%PDF-` magic header (so `looks_like_pdf` accepts it) plus a
    marker byte string a monkeypatched `has_financial_statements` can key
    its real-vs-decoy answer off, without needing an actual parseable PDF."""
    return b"%PDF-1.4\n%% " + marker + b"\n%%EOF"


def test_a_same_year_decoy_does_not_block_the_real_page_once_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without verify_content, the hub's own decoy PDF (dated 2025, same as
    the real fy2025 tab target - not yet visited) would satisfy "found
    enough years" and the crawl would stop before ever reaching the real
    page. With it, the decoy fails content verification, is never added to
    `result.candidates`, and the real fy2025 page still gets visited."""
    real_pdf = _pdf_bytes(b"real")
    decoy_pdf = _pdf_bytes(b"decoy")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path == "/":
            return httpx.Response(
                200,
                text='<html><body><a href="/investor-relations/annual-report">'
                "Investor Relations</a></body></html>",
            )
        if path == "/investor-relations/annual-report":
            return httpx.Response(
                200,
                text=(
                    "<html><body>"
                    '<a href="/docs/highlights-2025.pdf">2025 Highlights</a>'
                    '<a class="tab" href="/investor-relations/annual-report/fy2025">FY2025</a>'
                    "</body></html>"
                ),
            )
        if path == "/investor-relations/annual-report/fy2025":
            return httpx.Response(
                200,
                text='<html><body><a href="/docs/real-ar-2025.pdf">Annual Report 2025</a></body></html>',
            )
        if path == "/docs/highlights-2025.pdf":
            return httpx.Response(200, content=decoy_pdf, headers={"content-type": "application/pdf"})
        if path == "/docs/real-ar-2025.pdf":
            return httpx.Response(200, content=real_pdf, headers={"content-type": "application/pdf"})
        return httpx.Response(404)

    def fake_check(path: Path) -> bool:
        return b"real" in path.read_bytes()

    async def go():
        transport = httpx.MockTransport(handler)
        async with PoliteHttpClient(
            user_agent="TestBot/1.0", min_delay_seconds=0.0, transport=transport
        ) as client:
            return await sniff_annual_report(
                client, "https://example-bank.test/", min_years=1, verify_content=True
            )

    monkeypatch.setattr(ir_fallback_mod, "has_financial_statements", fake_check)
    result = run(go())

    urls = {c.url for c in result.candidates}
    assert "https://example-bank.test/docs/real-ar-2025.pdf" in urls
    assert "https://example-bank.test/docs/highlights-2025.pdf" not in urls


def test_verify_content_off_by_default_never_downloads_a_candidate() -> None:
    """The opt-in default (`verify_content=False`) must behave exactly as
    before - no download of any candidate during the crawl itself."""
    downloaded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if path == "/":
            return httpx.Response(
                200, text='<html><body><a href="/docs/report-2025.pdf">Report 2025</a></body></html>'
            )
        downloaded.append(path)
        return httpx.Response(200, content=_pdf_bytes(b"x"))

    async def go():
        transport = httpx.MockTransport(handler)
        async with PoliteHttpClient(
            user_agent="TestBot/1.0", min_delay_seconds=0.0, transport=transport
        ) as client:
            return await sniff_annual_report(client, "https://example-bank.test/")

    result = run(go())
    assert downloaded == []
    assert {c.year for c in result.candidates} == {2025}
