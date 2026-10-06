"""Given a company's IR homepage, find its most recent annual report PDF.

Grounded in real sites, not just synthetic fixtures - each one changed the
design, and the changes came from running the actual code against the live
page and checking the actual result, not from reasoning about the DOM:

* ``tnb.com.my`` - a flat hub page, one link per year, link text itself says
  "Annual Report for 2024" and the PDF filename is clean. The easy case.
* ``publicbankgroup.com`` - the hard case, found over several iterations:
  a year's report shares *one list* with its AGM notice, administrative
  details, and proxy form (not separate sections, as first assumed), and the
  label naming that list - a plain styled ``<div>``, not a semantic heading -
  sits as a sibling *before* the list, never a shared ancestor of its rows.
  Two false starts before this worked: (1) an unbounded "nearest preceding
  heading anywhere in the document" search misclassified an unrelated
  marketing PDF elsewhere on a *different* real site (see IHH below) - fixed
  by bounding the label search to a single *direct* sibling per ancestor
  level; (2) a character-length cap on "is this container too broad to
  trust" let a short-but-many-rows list (275 characters, six unrelated
  documents) through as if it were one document's own text - fixed by
  detecting a genuine multi-row container structurally
  (``_is_multi_row_container``) rather than by aggregate text length, which
  turned out not to correlate with "how many unrelated rows are blended
  together" at all.
* ``ihhhealthcare.com`` - the negative case worth keeping in mind: its real
  report list is rendered client-side, so a plain HTTP fetch sees zero `.pdf`
  hrefs at all. A ``NOT_FOUND`` here is honest, not a bug - and it's also
  where the unbounded-search false start above was first caught (it invented
  a match from an unrelated press-release PDF via a nearby "Annual Report"
  nav link's text).

Neither "match the link text alone" nor "grab every .pdf on the page" survives
the harder cases. What does: exclude a PDF by its *own* text first, and only
fall back to a directly-preceding sibling label - never a document-wide
search - combined with the row's own frozen text, never a container's
blended aggregate once that container is structurally a list of several rows.

**What changed after real extraction work downstream exposed a further real
gap**: link text alone - even with the label fallback above - cannot reliably
tell a *narrative-only* "annual report" apart from the actual filing that
carries the audited financial statements. Confirmed real on two more live
sites (CIMB, AMMB): each gave its real financial-statements volume its own
distinct heading that never says "annual report" anywhere, so the old
match-required classifier returned only the narrative volume and missed the
real one entirely - the exact failure this session's extraction work traced
back to a scraper gap, not a page-selection one. The fix is not a smarter
link-text rule (there is no reliable one to write): this module now casts a
much wider net - keep every same-year PDF link that isn't excluded outright
(a notice of AGM, a proxy form, administrative meeting papers - things that
can never be a financial statement, cheap to rule out from link text alone)
- and hands the real discrimination to `content_filter.has_financial_statements`,
which downloads each candidate and asks the same page-selection scorer used
for extraction whether it can actually find a primary statement inside.
Candidates are grouped by year first specifically so this only ever pits
same-year documents against each other - see `parts_for_best_year`.

This is explicitly heuristic at the link-collection stage - it will not
reliably work on every corporate website (a JS-rendered list is invisible to
it, by design - see the IHH case), and its failures are meant to be visible
(a ``NOT_FOUND`` in ``scrape_attempts``), not silently swallowed. The content
check downstream is the precision backstop; this module's job now is recall.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from bursa.scrapers.content_filter import has_financial_statements
from bursa.scrapers.http_client import PoliteHttpClient, RobotsDisallowed, looks_like_pdf

log = logging.getLogger(__name__)

# English + Malay. Deliberately loose - "MEDIA & INVESTORS" and "Investor
# Relations" both need to match, and neither site used the same label.
NAV_PATTERN = re.compile(
    r"investor|financial\s*report|annual\s*report|media\s*(&|and)?\s*investor|laporan\s*tahunan"
    r"|corporate\s*(?:info|profile|governance)|about\s*us|sustainability\s*report",
    re.IGNORECASE,
)
NAV_URL_PATTERN = re.compile(
    r"/investor|/annual[_-]?report|/ir/|/financial[_-]?report",
    re.IGNORECASE,
)
ANNUAL_REPORT_PATTERN = re.compile(
    r"annual\s*report|laporan\s*tahunan|integrated\s*report", re.IGNORECASE
)
# Same phrasing as ANNUAL_REPORT_PATTERN, checked against a URL path instead
# of rendered text - `\s*` alone never matches a real URL slug, which is
# hyphenated ("annual-report", "annual-reports"), not space-separated. Same
# lesson EXCLUDE_PATTERN's own docstring already documents for exactly this
# reason, re-learned here for `sniff_annual_report`'s year-nav URL gate
# (see below) after an early version of that gate silently never matched
# either of its two confirmed-real target sites.
ANNUAL_REPORT_URL_PATTERN = re.compile(
    r"annual[\s-]*report|laporan[\s-]*tahunan|integrated[\s-]*report", re.IGNORECASE
)
# The false-positive siblings seen on both real pages checked: a Notice of
# AGM, a Proxy Form, and administrative meeting documents live right next to
# the actual report. Quarterly Bursa announcements, analyst decks and call
# transcripts are a further real case (PETRONAS Chemicals): once link
# collection stopped requiring a positive "annual report" match, these
# sitting in the same year-dated folder as the real annual filing started
# passing through too - and a quarterly filing genuinely does contain real
# financial statements, so the post-download content check alone can't tell
# it apart from the annual one this scraper is specifically for. "1Q 2026"/
# "2Q 2026"/"quarterly" in the link's own text is a reliable, cheap signal
# to exclude before ever downloading it.
# Generic corporate-policy/governance documents are a further real case,
# found scraping a batch of 48 companies: several sites (a WordPress-style
# "wp-content/uploads" folder, or a listedcompany.com "misc" folder) list
# dozens of these right alongside - or instead of - the real annual report
# (confirmed real: Sarawak Oil Palms and Genting Plantations each yielded 24
# candidates, all policy documents, zero real filings). The content check
# downstream correctly never ingests any of these - nothing wrong ever got
# stored - but downloading and content-checking two dozen policy PDFs per
# company before concluding "not found" is real, avoidable waste. None of
# these phrasings ever legitimately titles a primary annual report or
# financial statements filing, so excluding them before download is safe.
# Separators are `[\s-]+`, not `\s+`: this pattern is checked against both
# rendered link text (usually space-separated) and, below, a raw URL/filename
# (almost always hyphen-separated, e.g. "Code-of-Business-Conduct.pdf") -
# a plain `\s+` silently never matches the hyphenated form at all, found by
# testing this exact pattern against a real observed decoy filename.
EXCLUDE_PATTERN = re.compile(
    r"notice[\s-]+of|proxy|circular|administrative|\bagm\b|\begm\b"
    r"|\bquarterly\b|\b[1-4]q\s*20\d\d\b|\bq[1-4]\s*20\d\d\b"
    r"|\banalyst\b|\btranscript\b|\bpresentation[\s-]*pack\b"
    r"|\bpolicy\b|\bpolicies\b|code[\s-]+of[\s-]+(business[\s-]+)?conduct"
    r"|\bcharter\b|terms?[\s-]+of[\s-]+reference|\btor\b|whistle[\s-]?blow"
    r"|\bpdpa\b|anti[\s-]?bribery|privacy[\s-]+(statement|notice)"
    r"|\bprospectus\b|\bbrochures?\b|\broadmaps?\b"
    # Investor-roadshow/conference decks - a real, distinct decoy class found
    # testing the scraper against prior years on Public Bank's site: these
    # sit in the same dated media folder as the real annual report and
    # genuinely contain enough tables/figures to pass the downstream content
    # check (has_financial_statements), so only link-text/filename exclusion
    # can catch them before download (e.g. "JPM ASEAN Financials Forum",
    # "Nomura ASEAN Conference", "UBS OneASEAN Summit").
    r"|\bconference\b|\bforum\b|\bsummit\b|\broadshow\b|investor[\s-]+day|\btour\b"
    # More investor-roadshow deck brandings, found testing a 5-year backfill
    # against the live Public Bank site: "Invest Malaysia", "[Bank] Corporate
    # Day", "Spotlight on Malaysia" - named conference/roadshow series, not
    # annual reports, each real and ingested before this fix.
    r"|invest[\s-]+(malaysia|asean)|corporate[\s-]+day|spotlight[\s-]+on",
    re.IGNORECASE,
)
YEAR_PATTERN = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")
# Unlike YEAR_PATTERN above (find a year anywhere in a blob of text), these
# are anchored with `fullmatch` in `find_year_nav_candidates` - the whole
# label must be the year (optionally "FY"-prefixed, AMMB's own tab wording)
# and nothing else, so "2024"/"FY2024" qualify but "English (est. 2024)" and
# "FY2024 (restated)" don't. `_ARCHIVE_LABEL` is deliberately separate, not
# folded into the same pattern - an "Archive" tab names no specific year at
# all, which is exactly why its match is handled differently by its caller.
_BARE_YEAR_LABEL = re.compile(r"\s*(?:fy\s*)?(19[89]\d|20[0-4]\d)\s*", re.IGNORECASE)
_ARCHIVE_LABEL = re.compile(r"\s*archives?\s*", re.IGNORECASE)

# Defaults for the four `sniff_annual_report` knobs below - real callers
# (`run_annual_reports.scrape_company`) pass the live `Settings` values
# instead, so the right depth for a given environment is a config change,
# never a code change (the right depth is a property of the *site* being
# crawled, confirmed to vary a lot - AMMB/Public Bank need real depth, most
# sites don't). These stay as plain module constants only so this file's own
# tests, and any direct caller that doesn't care, get sane behaviour for
# free without having to import or construct `Settings`.
MAX_HOPS = 5
HOP_ESCALATION_STEP = 2
MAX_HOPS_CEILING = 12
MAX_PAGES_PER_SNIFF = 60
MAX_LINKS_PER_HOP = 5
MAX_ANCESTOR_TEXT_CHARS = 400  # beyond this, the "nearby" walk stops trusting it
MAX_ANCESTOR_LEVELS = 5


@dataclass
class PdfCandidate:
    url: str
    matched_text: str
    year: int | None


@dataclass
class SniffResult:
    candidates: list[PdfCandidate] = field(default_factory=list)
    pages_visited: list[str] = field(default_factory=list)
    # True only when the homepage itself - not some deeper hop - was blocked
    # by robots.txt, meaning zero crawling could happen at all. A hop *past*
    # the homepage being blocked still counts as a genuine NOT_FOUND: real
    # crawling happened, it just didn't turn up a report.
    homepage_blocked_by_robots: bool = False

    @property
    def best_year(self) -> int | None:
        years = [c.year for c in self.candidates if c.year is not None]
        return max(years) if years else None

    def parts_for_best_year(self) -> list[PdfCandidate]:
        """Every candidate sharing the most recent detected year - deliberately
        grouped so a stray older-year PDF (a prior year's report still linked
        on the same page) never gets pitted against this year's real
        candidates. With link-text filtering now wide open (see module
        docstring), this can be several PDFs for one year - the narrative
        volume, the financial-statements volume, and whatever else wasn't
        excluded outright - and every one of them gets downloaded and passed
        to `content_filter.has_financial_statements` before anything is kept."""
        year = self.best_year
        if year is None:
            # No year detected anywhere - fall back to the single first
            # candidate found rather than guessing which of several is newest.
            return self.candidates[:1]
        return [c for c in self.candidates if c.year == year]


MAX_LABEL_SIBLING_CHARS = 80  # short enough to be a label, not a content blob


def _preceding_heading_sibling_text(node) -> str:  # type: ignore[no-untyped-def]
    """The text of ``node``'s *single, direct* preceding sibling, if that
    sibling reads as a short label rather than a content blob.

    Two things had to be got right here, both found by testing against a
    real page rather than a synthetic fixture:

    1. **Scope.** An early version used ``find_previous()``, which scans the
       entire rest of the document with no distance bound - on a real,
       content-heavy page it grabbed a heading arbitrarily far away and
       misclassified an unrelated marketing PDF as an annual report.
       Restricting this to the single direct preceding sibling of whichever
       ancestor level is currently being examined keeps the search bounded
       by the same ``MAX_ANCESTOR_LEVELS`` already governing the rest of the
       climb, never unbounded.
    2. **Tag name.** The obvious next assumption - require an ``h1``-``h6``
       tag - is also wrong on a real page: Public Bank's actual markup labels
       a document group with `<div class="subtitle">2025 Annual Report</div>`,
       not a semantic heading element. Real CMS templates routinely use a
       styled `<div>`/`<span>` instead. Any tag qualifies here; the length
       cap is what still tells a genuine short label apart from a large
       content sibling, which is what scope (1) protects against, not tag
       name.
    """
    sibling = node.find_previous_sibling()
    if sibling is None or sibling.name == node.name:
        # Same tag name as `node` itself almost always means "a peer item in
        # the same flat list" (e.g. another `<li>`), not a section label - a
        # real page's genuine label differs in tag from what it labels (a
        # `<div>` before a `<ul>`). Without this, an unrelated neighbouring
        # document's own exclude/include terms leak into this row's
        # classification purely because of list position - found by testing
        # against a fixture with more than two peer rows, where a document
        # sitting right after an excluded "Notice of AGM" row was wrongly
        # excluded too, just for being next to it.
        return ""
    text = sibling.get_text(" ", strip=True)
    return text if 0 < len(text) <= MAX_LABEL_SIBLING_CHARS else ""


def _is_multi_row_container(node, previous_tag_name: str | None) -> bool:  # type: ignore[no-untyped-def]
    """Whether ``node`` wraps more than one sibling of ``previous_tag_name``
    that each independently contain their own link - a real list of several
    distinct documents, not several parts of one row.

    This is the discriminator a character-length cap turned out not to be: a
    real page's document list can be short in aggregate (275 characters,
    nowhere near any sane cap) while still bundling six unrelated documents'
    worth of exclude/include terms together. Counting same-tag *siblings that
    each carry their own link* catches that regardless of aggregate length,
    while not over-firing on a single row's own internal structure - e.g. a
    label `<span>` and a download `<span>` inside one `<li>` are two spans,
    but only one of them has a link, so this correctly reports "not a list"
    for that level and "yes, a list" only once climbing reaches the `<ul>`
    that actually wraps several `<li>` rows each with their own link.
    """
    if previous_tag_name is None:
        return False
    with_a_link = 0
    for child in node.find_all(previous_tag_name, recursive=False):
        if child.find("a", href=True):
            with_a_link += 1
            if with_a_link > 1:
                return True
    return False


def _classify_link(tag) -> tuple[bool, str]:  # type: ignore[no-untyped-def]
    """Decide whether a PDF link should be treated as a candidate at all,
    checking one enclosing level at a time and excluding at the *first*
    decisive match.

    A link is **included by default** - unlike an earlier version of this
    function, it no longer also has to positively match "annual report"
    text anywhere. Real IR sites don't reliably give the genuine
    financial-statements volume that exact label: confirmed real on two
    live sites (CIMB, AMMB) where the old match-required version returned
    only a single candidate each - the narrative-only "Integrated Annual
    Report", with zero broader candidates - because neither site's DOM gave
    a distinctly-titled "financial statements"/"financial report" PDF any
    shared "annual report" label to inherit, the way Public Bank's page
    does. Only a document that's excluded here (a notice of AGM, a proxy
    form, administrative meeting papers) is dropped before download; telling
    a genuine financial-statements volume apart from a narrative-only report
    that merely shares its year is left to the post-download content check
    in `content_filter.py`, which can actually look inside the PDF instead
    of guessing from link text - see `sniff_annual_report`'s docstring for
    the resulting "download every year-matched candidate, filter by content"
    strategy this feeds into.

    Two signals, kept carefully separate:

    - ``text``: the current level's own (possibly aggregated) text, used for
      the direct exclude check - but only up to the level below the first
      one found to be a genuine multi-row container
      (``_is_multi_row_container``). Checking a multi-row container's own
      aggregate directly is simply the wrong operation regardless of its
      length: a short list of several terse, unrelated rows defeats any
      length cap just as easily as a long one, and blends every row's
      exclude terms together.
    - ``row_text``: frozen at the last level that was still a single row (not
      yet a detected list), and used - never the current container's blended
      aggregate - once a preceding-sibling label is found. This is what lets
      Public Bank's real page (a year's report bundled in one `<ul>` together
      with its AGM notice and proxy form) resolve correctly: the `<ul>`'s own
      text is never checked directly once it's recognised as a list, but its
      preceding `<div>` label combined with *this row's own* frozen text still
      correctly identifies the row without the list's other rows leaking in.

    The label check itself runs at every level regardless of whether that
    level's container was flagged multi-row - the label
    (`_preceding_heading_sibling_text`) sits at exactly the level where the
    aggregate also becomes untrustworthy (a `<div>` labelling a whole `<ul>`),
    so suppressing the label check there would throw away the one signal
    needed at exactly the level it's needed.
    """
    text = tag.get_text(" ", strip=True)
    if tag.get("title"):
        text = f"{text} {tag['title']}"
    row_text = text

    node = tag
    previous_tag_name: str | None = None
    is_list = False

    for _ in range(MAX_ANCESTOR_LEVELS + 1):
        if not is_list:
            if EXCLUDE_PATTERN.search(text):
                return False, text
            row_text = text

        heading = _preceding_heading_sibling_text(node)
        if heading:
            combined = f"{heading} {row_text}"
            if EXCLUDE_PATTERN.search(combined):
                return False, combined

        previous_tag_name = node.name
        node = node.parent
        if node is None or getattr(node, "name", None) in ("body", "html", "[document]"):
            break
        text = node.get_text(" ", strip=True)
        if is_list or len(text) > MAX_ANCESTOR_TEXT_CHARS:
            is_list = True  # once too broad to trust, never trust it again
        elif _is_multi_row_container(node, previous_tag_name):
            is_list = True

    return True, row_text


def _extract_year(text: str, url: str) -> int | None:
    # Prefer the year found in the associated text (the report's own heading)
    # over one found in the URL - Public Bank's PDF path has no year at all,
    # and a URL can contain an unrelated year (a copyright footer, a CDN path).
    matches = [int(m) for m in YEAR_PATTERN.findall(text)]
    if matches:
        return max(matches)
    # unquote first: a real CIMB filename encodes "FYE 31 December 2019" as
    # "FYE%2031%20December%202019" - searching the raw, still-percent-encoded
    # string finds "2031" (the "%20" before "31" supplies its leading "20"),
    # a higher, entirely spurious year that would have silently become the
    # site-wide "most recent year" and made `parts_for_best_year()` return
    # only that one 2019 filing. Confirmed real against the live URL.
    matches = [int(m) for m in YEAR_PATTERN.findall(unquote(url))]
    return max(matches) if matches else None


def find_pdf_candidates(html: str, page_url: str) -> list[PdfCandidate]:
    """Every PDF link on one page not excluded by its own nearby text or its
    URL - see `_classify_link` for why this is deliberately a wide net now,
    not a positive "annual report" match.

    The URL is also checked, not just rendered text: a policy/governance
    PDF's own display text on the page is unknown ahead of time and can be
    as generic as "Download", but its filename almost always self-describes
    ("Board-Charter-12.8.2026.pdf") - confirmed real, scraping a batch of 48
    companies, where several sites list dozens of these. Checking the URL
    too catches that case without needing the page's text to cooperate.
    """
    soup = BeautifulSoup(html, "lxml")
    candidates: list[PdfCandidate] = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        href_lower = href.lower()
        is_pdf_href = href_lower.split("?")[0].endswith(".pdf")
        is_insage_ar = "downloading.aspx" in href_lower and "reporttype=ar" in href_lower
        if not is_pdf_href and not is_insage_ar:
            continue
        if EXCLUDE_PATTERN.search(unquote(href)):
            continue

        included, matched_text = _classify_link(link)
        if not included:
            continue

        absolute = urljoin(page_url, href)
        candidates.append(
            PdfCandidate(
                url=absolute,
                matched_text=matched_text,
                year=_extract_year(matched_text, absolute),
            )
        )

    return candidates


def find_nav_candidates(html: str, page_url: str, *, is_homepage: bool = False) -> list[str]:
    """Nav links worth following one hop deeper, ranked most-specific first.

    Matches on both link text AND href URL path so that dropdown/menu
    items with generic text but IR-specific URLs are still followed.

    When ``is_homepage`` is True (hop 0), only high-confidence IR links
    (score >= 2) are returned if any exist — skipping generic "About Us"
    / "Corporate Governance" links that waste hops on the way to the
    actual IR page.
    """
    soup = BeautifulSoup(html, "lxml")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        text = link.get_text(" ", strip=True)
        href = link["href"]
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        text_match = bool(text and NAV_PATTERN.search(text))
        url_match = bool(NAV_URL_PATTERN.search(href))
        if not text_match and not url_match:
            continue

        absolute = urljoin(page_url, href)
        if urlsplit(absolute).netloc != urlsplit(page_url).netloc:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)

        score = 0
        if text and ANNUAL_REPORT_PATTERN.search(text):
            score = 3
        elif url_match and text_match:
            score = 2
        elif url_match:
            score = 2
        else:
            score = 1
        scored.append((score, absolute))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    if is_homepage:
        high = [url for s, url in scored if s >= 2]
        if high:
            return high[:MAX_LINKS_PER_HOP]

    return [url for _, url in scored[:MAX_LINKS_PER_HOP]]


def find_year_nav_candidates(html: str, page_url: str) -> list[tuple[int | None, str]]:
    """Every server-rendered, year-labelled navigational element worth
    following when the crawl is still short on distinct years - broader
    than `find_nav_candidates` above, which only matches "investor"/"annual
    report"/... *phrasing* and so misses a label that's just a year with no
    other words at all.

    Two real, independently confirmed-live shapes fall under this one
    umbrella, because both are the same underlying thing: a year-switcher a
    page never phrases as "annual report" anywhere, so the generic nav
    classifier above never follows it, even though nothing about either
    needs JavaScript to see - a plain HTTP GET already returns the full
    markup, it was simply never looked at before now.

    - **A ``<select>``'s own ``<option>``** - publicbankgroup.com's "Select
      Year" dropdown. Every option is fully server-rendered and
      independently fetchable (confirmed:
      ``/investor-relations/annual-reports/2019-annual-report/`` returns a
      real 200 with that year's own report), despite looking at first
      glance like a dynamic, JS-only picker.
    - **A sliding tab bar's own ``<a>``**, labelled "FY2025" or similar -
      ambankgroup.com's year-tab carousel. Confirmed to be an ordinary
      ``<a href>`` the whole time; the only reason `find_nav_candidates`
      missed it is that "FY2025" matches none of `NAV_PATTERN`'s phrasings.
      The same tab bar's own "Archive" tab is a third shape worth folding
      in here rather than treating as a one-off: a single link bundling
      every year older than the tabs bother to list individually
      (confirmed real on AMMB - one page holding 2004-2011 outright, no
      further pagination needed). Returned with ``year=None`` since its own
      label names no specific year, so the caller should always consider it
      while still short rather than try to match it against one.

    Returned newest-year-first (any ``year=None`` archive-style entries
    last, since a real one has so far only ever turned out to cover years
    *older* than whatever the individual tabs already list) - a caller only
    short a handful of years can take just that many instead of queuing
    every year (or archive) a page has ever offered.
    """
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[int | None, str]] = []
    seen: set[str] = set()

    def consider(href_value: str | None, text: str) -> None:
        href_value = (href_value or "").strip()
        if not href_value or href_value.startswith(("#", "javascript:")):
            return
        absolute = urljoin(page_url, href_value)
        if urlsplit(absolute).netloc != urlsplit(page_url).netloc or absolute in seen:
            return
        if not ANNUAL_REPORT_URL_PATTERN.search(absolute):
            # The label alone ("FY2025", "2025") isn't enough on its own -
            # confirmed real on AMMB, whose annual-report hub page *itself*
            # also embeds a sitewide "related year" footer duplicating every
            # year across several unrelated sections (financial results,
            # investor-relations calendar, company awards) - each exactly as
            # bare-year-labelled as the hub's own genuine tab bar, several
            # sharing the very same year so a per-page gate on *which page*
            # to scan (`ANNUAL_REPORT_URL_PATTERN.search(response.final_url)`
            # at this function's call site) can't tell them apart on its
            # own. The one thing that reliably does: the candidate's *own*
            # target URL, not just the label or the page it was found on -
            # true on both confirmed-real shapes (Public Bank's dropdown
            # options, AMMB's real tabs) but not on either site's unrelated
            # same-year lookalikes.
            return

        year_match = _BARE_YEAR_LABEL.fullmatch(text)
        if year_match:
            seen.add(absolute)
            found.append((int(year_match.group(1)), absolute))
        elif _ARCHIVE_LABEL.fullmatch(text):
            seen.add(absolute)
            found.append((None, absolute))
        # Anything else (a label that merely *mentions* a year, like
        # "English (est. 2024)" or "FY2024 (restated)") is deliberately not
        # this pattern - see test_find_year_option_candidates_ignores_a_
        # dropdown_whose_label_is_not_bare for exactly why `fullmatch`
        # against the *whole* label matters here, not just "contains one
        # year": an earlier version of this check only required finding
        # exactly one year mention anywhere in the text, which let a label
        # that merely mentioned one through as if it were a year switcher.

    for option in soup.find_all("option"):
        consider(option.get("value"), option.get_text(strip=True))
    for link in soup.find_all("a", href=True):
        consider(link["href"], link.get_text(strip=True))

    found.sort(key=lambda pair: (pair[0] is None, -(pair[0] or 0)))
    return found


async def _verify_candidates(
    client: PoliteHttpClient, candidates: list[PdfCandidate]
) -> list[PdfCandidate]:
    """Download and content-check each candidate, keeping only the ones that
    actually have a primary statement inside - the fix for a real gap found
    verifying the dynamic hop/year-nav logic above against live sites: a
    decoy that merely happens to be dated the same year as a real,
    not-yet-visited page (confirmed real on AMMB - a homepage coffee-table-
    book PDF dated 2025) used to satisfy "found enough distinct years"
    before the real page for that year was ever reached, since year-counting
    trusted a candidate's printed/linked year alone. Verifying content
    *before* a candidate's year is allowed to count closes that gap at the
    source: a decoy is simply never added to `result.candidates` at all,
    so it can never occupy a year's slot that a real document should.

    Not free - this downloads every candidate during the crawl itself, not
    only the ones a caller ultimately keeps, and a kept one gets downloaded
    again later by `run_annual_reports._scrape_one_year`'s own ingest pass
    (this function has no way to hand its own download back across that
    boundary). Opt-in via `sniff_annual_report`'s own `verify_content`
    parameter for exactly that reason - `scrape_company` only turns it on
    for a real (non-dry-run) scrape, preserving dry-run's existing contract
    of never downloading anything at all.
    """
    verified: list[PdfCandidate] = []
    for candidate in candidates:
        try:
            response = await client.get(candidate.url)
        except RobotsDisallowed:
            log.info("robots.txt disallows %s; treating as unverified", candidate.url)
            continue
        except Exception as exc:
            log.warning("failed to fetch %s for content verification: %s", candidate.url, exc)
            continue

        if not response.ok or not looks_like_pdf(response.content):
            continue

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = Path(tmp.name)
        try:
            if has_financial_statements(tmp_path):
                verified.append(candidate)
        finally:
            tmp_path.unlink(missing_ok=True)

    return verified


async def sniff_annual_report(
    client: PoliteHttpClient,
    homepage_url: str,
    *,
    min_years: int = 1,
    base_hops: int = MAX_HOPS,
    hop_escalation_step: int = HOP_ESCALATION_STEP,
    max_hops_ceiling: int = MAX_HOPS_CEILING,
    max_pages: int = MAX_PAGES_PER_SNIFF,
    verify_content: bool = False,
) -> SniffResult:
    """Crawl from a company's homepage looking for its newest annual report
    PDF(s), going deep enough to find ``min_years`` distinct years if the
    site's own nav allows it.

    ``verify_content=True`` downloads and content-checks every candidate
    (`_verify_candidates`) before it's allowed to occupy a year - without
    this, a decoy coincidentally dated the same year as a real,
    not-yet-visited page can satisfy "found enough years" first and stop the
    crawl short (confirmed real on AMMB). Off by default and only worth
    turning on for a real (non-dry-run) scrape - it downloads every
    candidate found during the crawl itself, not only the ones ultimately
    kept, which a plain sniff (no downloads at all) never does.

    The hop limit is dynamic, not fixed: crawling starts with a budget of
    ``base_hops`` and only grows - never shrinks - when, after exhausting the
    current budget, fewer than ``min_years`` distinct years have been found
    *and* there is still an unvisited frontier to follow. Confirmed real need
    for this: AMMB's and Public Bank's own nav structure puts older years'
    reports behind more hops than a "newest only" request ever needs, so a
    flat hop limit generous enough for that silently starved a `--years 5`
    backfill down to 1 year on those sites - with nothing short of counting
    documents afterwards to reveal it.

    Growing the budget is deliberately proportional to how far short of the
    target the crawl still is (at least ``hop_escalation_step`` hops, more
    when clearly further off - e.g. 1 year found against a target of 6 jumps
    the budget by 5, not a timid +2 that would take three more rounds to
    reach the same depth on a site whose archive is paginated one year per
    hop), which is what makes this "dynamic" rather than "a slightly bigger
    hardcoded number": the step adapts to the shape of whatever site is
    being crawled instead of assuming one fixed depth works for all of them.

    Three independent guards keep this provably finite regardless of how
    pathological a real site's nav turns out to be, each catching a
    different failure shape:

    - ``visited`` is shared across every escalation, so a page already
      fetched is never fetched again - a cycle in the site's own nav (one
      archive page linking back to an earlier one) can never cause a repeat
      fetch, let alone an infinite one.
    - ``max_hops_ceiling`` is an absolute ceiling no shortfall can push the
      budget past, regardless of how many years are still missing.
    - ``max_pages`` bounds total fetches directly, independent of hop count -
      the backstop against a site whose nav simply keeps offering new,
      never-before-seen URLs at every hop (no cycle for ``visited`` to catch)
      without ever supplying enough distinct years.
    """
    result = SniffResult()
    to_visit = [homepage_url]
    visited: set[str] = set()
    max_hops = base_hops
    hop = 0

    while True:
        next_hop: list[str] = []
        for url in to_visit:
            if url in visited or len(visited) >= max_pages:
                continue
            visited.add(url)

            try:
                response = await client.get(url)
            except RobotsDisallowed:
                log.info("robots.txt disallows %s; skipping", url)
                if url == homepage_url:
                    result.homepage_blocked_by_robots = True
                continue
            except Exception as exc:
                log.warning("failed to fetch %s: %s", url, exc)
                continue

            if not response.ok:
                log.info("%s returned %s; skipping", url, response.status_code)
                continue

            result.pages_visited.append(url)
            html = response.text()
            page_candidates = find_pdf_candidates(html, response.final_url)
            if verify_content:
                page_candidates = await _verify_candidates(client, page_candidates)
            result.candidates.extend(page_candidates)

            next_hop.extend(find_nav_candidates(html, response.final_url, is_homepage=(hop == 0)))

            # A year-labelled dropdown option or tab (see
            # `find_year_nav_candidates`) is a strong signal, but only on a
            # page that is itself plausibly about annual reports - gated on
            # the page's own URL matching `ANNUAL_REPORT_PATTERN`, cheaply
            # excluding the rest of a site without parsing anything extra.
            # Confirmed real need on AMMB's own homepage (its own
            # `ir_homepage_url`, not a dedicated IR landing page the way
            # Public Bank's happens to be): it carries *several* unrelated
            # year-tab widgets of its own ("Financial Results &
            # Presentations", an awards page, an investor-relations
            # calendar), each just as bare-year-labelled as the real
            # annual-report hub's tab bar, and a plain "visit real nav links
            # first" ordering (tried first, kept above since it's still a
            # real improvement on its own) wasn't enough on its own - those
            # unrelated pages still got visited and still polluted `have`
            # with unrelated years, in one observed case even a year that
            # happened to coincide with a real still-missing one, wrongly
            # marking it "already covered" before the genuine page for it
            # was ever reached. A real annual-report hub's own URL has
            # always said so in every case found so far (confirmed on both
            # Public Bank's and AMMB's) - a site that keeps its year archive
            # at a URL that never mentions "annual report" at all would miss
            # this gate, but no such site has been found yet, and the
            # alternative (no gate) is the confirmed-worse AMMB behaviour
            # this fixes.
            year_nav = (
                find_year_nav_candidates(html, response.final_url)
                if ANNUAL_REPORT_URL_PATTERN.search(response.final_url)
                else []
            )
            if year_nav:
                have = {c.year for c in result.candidates if c.year is not None}
                still_needed = max(0, min_years - len(have))
                missing = [url for year, url in year_nav if year not in have]
                next_hop.extend(missing[:still_needed])

        distinct_years = {c.year for c in result.candidates if c.year is not None}
        # "Enough" either means real, dated candidates covering as many
        # distinct years as asked for, or - the original, years-agnostic
        # shape - some candidates with no year on any of them at all (the
        # `parts_for_best_year()` single-candidate fallback this feeds).
        enough = bool(result.candidates) and (not distinct_years or len(distinct_years) >= min_years)

        # Never trust hop 0's raw entry homepage alone, "enough" or not. Real
        # sites routinely scatter a stray PDF link or two directly on the
        # homepage (a privacy policy, an FAQ, a coffee-table book) that
        # satisfies "found candidates" under this deliberately wide link
        # collection (see module docstring) - confirmed real on two live
        # sites (CIMB, AMMB), both stopping at the homepage on an unrelated
        # PDF before ever following the nav link to the real reports page.
        # No real annual-report listing was ever found sitting directly on a
        # homepage across every site checked this project - always at least
        # one nav hop away - so requiring hop >= 1 costs nothing on a
        # genuine hit and fixes the false one.
        if hop > 0 and enough:
            break
        if not next_hop or len(visited) >= max_pages:
            break
        if hop >= max_hops:
            if max_hops >= max_hops_ceiling:
                break
            shortfall = max(1, min_years - len(distinct_years))
            new_max_hops = min(max_hops_ceiling, max_hops + max(hop_escalation_step, shortfall))
            if new_max_hops == max_hops:
                break  # already pinned at the ceiling - escalating further gains nothing
            log.info(
                "escalating IR crawl hop limit %d -> %d for %s (%d/%d distinct years found so far)",
                max_hops, new_max_hops, homepage_url, len(distinct_years), min_years,
            )
            max_hops = new_max_hops

        to_visit = next_hop
        hop += 1

    return result
