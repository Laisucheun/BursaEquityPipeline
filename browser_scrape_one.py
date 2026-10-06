"""Browser-scrape a single company. Called as subprocess to avoid OOM.
Usage: python browser_scrape_one.py <stock_code> <pdf_dir>
Prints result line: FOUND|NOT_FOUND|TIMEOUT|ERROR [detail]
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, parse_qs, urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

sys.path.insert(0, "src")
from bursa.db.session import session_scope
from bursa.db.models import Company, Document
from bursa.db.enums import DocSource
from bursa.pipeline.ingest import ingest_file
from bursa.scrapers.content_filter import has_financial_statements
from sqlalchemy import select
import httpx

ANNUAL_RE = re.compile(
    r"annual[\s_-]*report|integrated[\s_-]*report|laporan[\s_-]*tahunan",
    re.IGNORECASE,
)
IR_RE = re.compile(
    r"investor|financial[\s_-]*(?:info|report)|annual[\s_-]*report|laporan",
    re.IGNORECASE,
)
IR_URL_RE = re.compile(
    r"/investor|/ir/|/annual[_-]?report|/financial[_-]?report",
    re.IGNORECASE,
)
EXCLUDE_RE = re.compile(
    r"quarter|interim|proxy|notice|circular|agm|governance|sustainability",
    re.IGNORECASE,
)


def extract_pdf_links(page):
    """Extract all PDF candidate links from current page (including iframes)."""
    candidates = []
    try:
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: e.textContent.trim().substring(0, 120)}))"
        )
    except Exception:
        links = []

    for link in links:
        href = link["href"]
        text = link["text"]
        is_pdf = href.lower().endswith(".pdf")
        is_insage = "downloading.aspx" in href.lower()
        is_tracker = "tracker.pl" in href.lower()
        if not (is_pdf or is_insage or is_tracker):
            continue
        if EXCLUDE_RE.search(text) and not EXCLUDE_RE.search(href):
            if EXCLUDE_RE.search(text):
                continue
        score = 0
        if ANNUAL_RE.search(text) or ANNUAL_RE.search(href):
            score = 3
        elif is_insage or is_tracker:
            score = 2
        elif is_pdf:
            score = 1
        candidates.append({"url": href, "text": text, "score": score})

    # Also check iframes for PDF links
    try:
        frames = page.frames
        for frame in frames:
            if frame == page.main_frame:
                continue
            try:
                frame_links = frame.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({href: e.href, text: e.textContent.trim().substring(0, 120)}))"
                )
                for link in frame_links:
                    href = link["href"]
                    text = link["text"]
                    is_pdf = href.lower().endswith(".pdf")
                    is_insage = "downloading.aspx" in href.lower()
                    is_tracker = "tracker.pl" in href.lower()
                    if not (is_pdf or is_insage or is_tracker):
                        continue
                    if EXCLUDE_RE.search(text) and EXCLUDE_RE.search(href):
                        continue
                    score = 0
                    if ANNUAL_RE.search(text) or ANNUAL_RE.search(href):
                        score = 3
                    elif is_insage or is_tracker:
                        score = 2
                    elif is_pdf:
                        score = 1
                    candidates.append({"url": href, "text": text, "score": score})
            except Exception:
                pass
    except Exception:
        pass

    candidates.sort(key=lambda c: -c["score"])
    return candidates


def find_ir_links(page):
    """Find IR/annual reports sub-page links."""
    try:
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: e.textContent.trim().substring(0, 80)}))"
        )
    except Exception:
        return []

    scored = []
    seen = set()
    for link in links:
        href = link["href"]
        text = link["text"]
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        if href in seen:
            continue
        seen.add(href)
        score = 0
        if ANNUAL_RE.search(text) or ANNUAL_RE.search(href):
            score = 3
        elif IR_RE.search(text):
            score = 2
        elif IR_URL_RE.search(href):
            score = 2
        if score > 0:
            scored.append((score, href))

    scored.sort(key=lambda x: -x[0])
    return [url for _, url in scored[:4]]


def download_pdf(url, dest_path, timeout=45):
    """Download a PDF file, following redirects."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        with httpx.stream("GET", url, headers=headers, timeout=timeout, follow_redirects=True) as r:
            if r.status_code != 200:
                return False
            ct = r.headers.get("content-type", "")
            with open(dest_path, "wb") as f:
                for chunk in r.iter_bytes(8192):
                    f.write(chunk)
        with open(dest_path, "rb") as f:
            header = f.read(5)
        if not header.startswith(b"%PDF"):
            dest_path.unlink(missing_ok=True)
            return False
        return True
    except Exception:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        return False


def make_safe_filename(url):
    """Create a safe filename from a URL."""
    raw = url.rsplit("/", 1)[-1]
    if "?" in raw:
        qs = parse_qs(urlsplit(url).query)
        raw = qs.get("sFileName", [raw.split("?")[0]])[0]
    safe = re.sub(r'[<>:"/\\|?*]', "_", raw)[:120]
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    return safe or "report.pdf"


def scrape_one(stock_code, pdf_dir):
    with session_scope() as s:
        company = s.scalar(select(Company).where(Company.stock_code == stock_code))
        if not company:
            return "SKIP", "not in DB"
        if not company.ir_homepage_url:
            return "NO_URL", None

        url = company.ir_homepage_url
        company_id = company.id

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        try:
            page.goto(url, timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        except PwTimeout:
            browser.close()
            return "TIMEOUT", url
        except Exception as e:
            browser.close()
            return "ERROR", str(e)[:100]

        # Step 1: Look for PDFs on current page (+ iframes)
        candidates = extract_pdf_links(page)

        # Step 2: If no good candidates, follow IR sub-links (up to 2 hops)
        if not any(c["score"] >= 2 for c in candidates):
            ir_links = find_ir_links(page)
            for ir_url in ir_links[:3]:
                if ir_url == url:
                    continue
                try:
                    page.goto(ir_url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    new_candidates = extract_pdf_links(page)
                    if any(c["score"] >= 2 for c in new_candidates):
                        candidates = new_candidates
                        break
                    elif new_candidates and not candidates:
                        candidates = new_candidates
                except Exception:
                    pass

        browser.close()

        if not candidates:
            return "NOT_FOUND", None

        # Step 3: Download best candidates
        best = sorted(candidates, key=lambda c: -c["score"])
        company_dir = Path(pdf_dir) / stock_code
        company_dir.mkdir(parents=True, exist_ok=True)

        for candidate in best[:5]:
            pdf_url = candidate["url"]
            safe_name = make_safe_filename(pdf_url)
            dest = company_dir / safe_name

            if download_pdf(pdf_url, dest):
                if has_financial_statements(dest):
                    with session_scope() as s:
                        ingest_file(s, dest, DocSource.IR, company_id=company_id)
                        s.commit()
                    return "FOUND", pdf_url
                else:
                    dest.unlink(missing_ok=True)

        return "NOT_FOUND", f"tried {len(best[:5])} PDFs, none had financial statements"


if __name__ == "__main__":
    stock_code = sys.argv[1]
    pdf_dir = sys.argv[2] if len(sys.argv) > 2 else "pdfs"
    status, detail = scrape_one(stock_code, pdf_dir)
    print(f"{status} {detail or ''}")
