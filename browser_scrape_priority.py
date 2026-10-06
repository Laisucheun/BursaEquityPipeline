"""Targeted browser scraper for high-priority large/mid cap companies.
More aggressive: longer waits, more hops, relaxed content filter, manual URL fixes.
Usage: python browser_scrape_priority.py
"""
import re
import sys
import json
from pathlib import Path
from urllib.parse import urljoin, parse_qs, urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
import httpx

sys.path.insert(0, "src")
from bursa.db.session import session_scope
from bursa.db.models import Company, Document
from bursa.db.enums import DocSource
from bursa.pipeline.ingest import ingest_file
from bursa.scrapers.content_filter import has_financial_statements
from sqlalchemy import select

ANNUAL_RE = re.compile(
    r"annual[\s_-]*report|integrated[\s_-]*report|laporan[\s_-]*tahunan",
    re.IGNORECASE,
)
IR_RE = re.compile(
    r"investor|financial[\s_-]*(?:info|report|result)|annual[\s_-]*report|laporan|corporate[\s_-]*report",
    re.IGNORECASE,
)

# Manual URL overrides for known problematic companies
URL_OVERRIDES = {
    "1155": "https://www.maybank.com/en/investor-relations/financial-overview.page",
    "4707": "https://www.nestle.com.my/investors/annual_report",
    "7113": "https://www.topglove.com/investor-relations/annual-reports",
    "8664": "https://spsetia.com.my/investor-relations",
    "5296": "https://corporate.mrdiy.com/ar.html",
    "5148": "https://www.uemsunrise.com/investor-relations/annual-reports",
    "8583": "https://www.mahsing.com.my/investor-relations/",
    "5031": "https://corporate.time.com.my/investor-relations/annual-reports/",
}


def extract_all_pdf_links(page):
    """Extract PDF links from page + all iframes."""
    candidates = []

    def process_links(links):
        for link in links:
            href = link.get("href", "")
            text = link.get("text", "")
            is_pdf = href.lower().endswith(".pdf")
            is_dl = "downloading.aspx" in href.lower() or "tracker.pl" in href.lower()
            is_download = "download" in href.lower() and ("report" in href.lower() or "annual" in href.lower())
            if not (is_pdf or is_dl or is_download):
                continue
            score = 0
            if ANNUAL_RE.search(text) or ANNUAL_RE.search(href):
                score = 3
            elif is_dl:
                score = 2
            elif is_pdf:
                score = 1
            candidates.append({"url": href, "text": text, "score": score})

    try:
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: e.textContent.trim().substring(0, 150)}))"
        )
        process_links(links)
    except Exception:
        pass

    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                frame_links = frame.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({href: e.href, text: e.textContent.trim().substring(0, 150)}))"
                )
                process_links(frame_links)
            except Exception:
                pass
    except Exception:
        pass

    candidates.sort(key=lambda c: -c["score"])
    return candidates


def find_ir_sublinks(page):
    """Find IR/annual report sub-page links."""
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
        elif re.search(r"/investor|/ir/|/annual|/financial", href, re.IGNORECASE):
            score = 2
        if score > 0:
            scored.append((score, href))

    scored.sort(key=lambda x: -x[0])
    return [url for _, url in scored[:6]]


def download_pdf(url, dest_path, timeout=60):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        with httpx.stream("GET", url, headers=headers, timeout=timeout, follow_redirects=True) as r:
            if r.status_code != 200:
                return False
            with open(dest_path, "wb") as f:
                for chunk in r.iter_bytes(8192):
                    f.write(chunk)
        with open(dest_path, "rb") as f:
            if not f.read(5).startswith(b"%PDF"):
                dest_path.unlink(missing_ok=True)
                return False
        if dest_path.stat().st_size < 50_000:
            dest_path.unlink(missing_ok=True)
            return False
        return True
    except Exception:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        return False


def make_safe_filename(url):
    raw = url.rsplit("/", 1)[-1]
    if "?" in raw:
        qs = parse_qs(urlsplit(url).query)
        raw = qs.get("sFileName", [raw.split("?")[0]])[0]
    safe = re.sub(r'[<>:"/\\|?*]', "_", raw)[:120]
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    return safe or "report.pdf"


def scrape_company(page, code, name, url, pdf_dir):
    """Aggressively scrape one company."""
    print(f"  Loading {url[:70]}...")

    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
    except PwTimeout:
        return "TIMEOUT", None
    except Exception as e:
        return "ERROR", str(e)[:80]

    # Step 1: Check current page
    candidates = extract_all_pdf_links(page)

    # Step 2: Follow IR sub-links (up to 3 hops)
    if not any(c["score"] >= 2 for c in candidates):
        ir_links = find_ir_sublinks(page)
        for ir_url in ir_links[:4]:
            if ir_url == url:
                continue
            print(f"    Following: {ir_url[:70]}...")
            try:
                page.goto(ir_url, timeout=25000, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                new_cands = extract_all_pdf_links(page)
                if any(c["score"] >= 2 for c in new_cands):
                    candidates = new_cands
                    break
                # On the IR page, look for deeper "annual reports" link
                if not any(c["score"] >= 2 for c in new_cands):
                    deeper = find_ir_sublinks(page)
                    for d_url in deeper[:3]:
                        if d_url == ir_url or d_url == url:
                            continue
                        if re.search(r"annual|report|ar\.html", d_url, re.IGNORECASE):
                            print(f"    Deeper: {d_url[:70]}...")
                            try:
                                page.goto(d_url, timeout=25000, wait_until="domcontentloaded")
                                page.wait_for_timeout(4000)
                                deep_cands = extract_all_pdf_links(page)
                                if deep_cands:
                                    candidates = deep_cands
                                    break
                            except Exception:
                                pass
                if candidates:
                    break
            except Exception:
                pass

    if not candidates:
        return "NOT_FOUND", "no PDF links found"

    # Step 3: Download best candidates (try more)
    best = sorted(candidates, key=lambda c: -c["score"])
    company_dir = pdf_dir / code
    company_dir.mkdir(parents=True, exist_ok=True)

    for candidate in best[:8]:
        pdf_url = candidate["url"]
        safe_name = make_safe_filename(pdf_url)
        dest = company_dir / safe_name
        print(f"    Trying: {candidate['text'][:50]} ({candidate['score']})")

        if download_pdf(pdf_url, dest):
            # For large/mid caps, accept any PDF > 500KB even without financial statements keyword
            size_mb = dest.stat().st_size / (1024 * 1024)
            if has_financial_statements(dest):
                return "FOUND", dest
            elif size_mb > 0.5 and candidate["score"] >= 2:
                # Likely an annual report even if content filter misses it
                return "FOUND_RELAXED", dest
            else:
                dest.unlink(missing_ok=True)

    return "NOT_FOUND", f"tried {min(len(best), 8)} PDFs, none qualified"


def main():
    pdf_dir = Path("pdfs")
    log_file = "priority_scrape_log.txt"

    with open("scrape_priority.txt") as f:
        codes = [line.strip() for line in f if line.strip()]

    with session_scope() as s:
        has_docs = set(r[0] for r in s.execute(select(Document.company_id).distinct()).all())
        companies_map = {}
        for c in s.execute(select(Company)).scalars().all():
            companies_map[c.stock_code] = (c.id, c.name, c.ir_homepage_url)

    # Filter to only missing companies
    missing = []
    for code in codes:
        if code not in companies_map:
            continue
        cid, name, url = companies_map[code]
        if cid in has_docs:
            continue
        if code in URL_OVERRIDES:
            url = URL_OVERRIDES[code]
        missing.append((code, name, url, cid))

    print(f"Priority scraping {len(missing)} large/mid cap companies\n")
    found = not_found = errors = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        for i, (code, name, url, cid) in enumerate(missing):
            print(f"[{i+1}/{len(missing)}] {code} {name[:30]}")

            if not url:
                status = "NO_URL"
                errors += 1
            else:
                status, result = scrape_company(page, code, name, url, pdf_dir)

                if status in ("FOUND", "FOUND_RELAXED") and result:
                    try:
                        with session_scope() as s:
                            ingest_file(s, result, DocSource.IR, company_id=cid)
                            s.commit()
                        found += 1
                    except Exception as e:
                        status = f"INGEST_ERROR({e})"
                        errors += 1
                elif status == "NOT_FOUND":
                    not_found += 1
                else:
                    errors += 1

            line = f"[{i+1}/{len(missing)}] {code} {name[:25]:25s} -> {status}\n"
            print(f"  => {status}\n")
            with open(log_file, "a") as lf:
                lf.write(line)

        browser.close()

    final = f"\nDone: found={found} not_found={not_found} errors={errors} total={len(missing)}"
    print(final)
    with open(log_file, "a") as f:
        f.write(final + "\n")


if __name__ == "__main__":
    main()
