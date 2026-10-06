"""Scan listedcompany.com for remaining companies' annual reports.
Many Malaysian companies have IR pages at {shortname}.listedcompany.com.
Usage: python listedcompany_scan.py [pdfs_dir]
"""
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

sys.path.insert(0, "src")
from bursa.db.session import session_scope
from bursa.db.models import Company, Document
from bursa.db.enums import DocSource
from bursa.pipeline.ingest import ingest_file
from bursa.scrapers.content_filter import has_financial_statements
from sqlalchemy import select, func

ANNUAL_RE = re.compile(
    r"annual[\s_-]*report|integrated[\s_-]*report|laporan[\s_-]*tahunan",
    re.IGNORECASE,
)


def make_shortnames(company):
    """Generate possible listedcompany.com subdomain names from company name."""
    name = company.name.lower()
    # Remove common suffixes
    for suffix in ["berhad", "bhd", "holdings", "group", "corporation", "corp",
                   "industries", "industrial", "technology", "tech", "resources",
                   "capital", "property", "properties", "development", "reit",
                   "international", "global", "(malaysia)", "sdn", "limited", "ltd"]:
        name = name.replace(suffix, "")
    name = name.strip(" .-,")
    parts = name.split()

    candidates = []
    # Full name joined
    if parts:
        candidates.append("".join(parts))
        candidates.append("-".join(parts))
        # First word only
        if len(parts[0]) >= 3:
            candidates.append(parts[0])
        # First two words
        if len(parts) >= 2:
            candidates.append("".join(parts[:2]))
            candidates.append("-".join(parts[:2]))

    # Also try stock code
    candidates.append(company.stock_code)

    # If company already has a listedcompany URL, extract the subdomain
    url = company.ir_homepage_url or ""
    if "listedcompany.com" in url:
        m = re.search(r'https?://([^.]+)\.listedcompany\.com', url)
        if m:
            candidates.insert(0, m.group(1))
        m2 = re.search(r'https?://([^.]+)-assistive\.listedcompany\.com', url)
        if m2:
            candidates.insert(0, m2.group(1))

    return list(dict.fromkeys(candidates))  # dedupe preserving order


def download_pdf(url, dest_path, timeout=45):
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
        return True
    except Exception:
        if dest_path.exists():
            dest_path.unlink(missing_ok=True)
        return False


def check_listedcompany(page, shortname):
    """Check if {shortname}.listedcompany.com has annual reports."""
    base_url = f"https://{shortname}.listedcompany.com"
    ar_url = f"{base_url}/ar.html"

    try:
        resp = page.goto(ar_url, timeout=15000, wait_until="domcontentloaded")
        if not resp or resp.status >= 400:
            return None
        page.wait_for_timeout(2000)
    except Exception:
        return None

    # Check page title — invalid subdomains often redirect to generic page
    title = page.title().lower()
    if "not found" in title or "error" in title:
        return None

    # Extract PDF links
    try:
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: e.textContent.trim().substring(0, 120)}))"
        )
    except Exception:
        return None

    pdf_links = []
    for link in links:
        href = link["href"]
        text = link["text"]
        if not (href.lower().endswith(".pdf") or "tracker.pl" in href.lower()):
            continue
        if ANNUAL_RE.search(text) or ANNUAL_RE.search(href):
            pdf_links.append({"url": href, "text": text, "score": 2})
        else:
            pdf_links.append({"url": href, "text": text, "score": 1})

    # Also check frames
    try:
        for frame in page.frames:
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
                    if not (href.lower().endswith(".pdf") or "tracker.pl" in href.lower()):
                        continue
                    if ANNUAL_RE.search(text) or ANNUAL_RE.search(href):
                        pdf_links.append({"url": href, "text": text, "score": 2})
                    else:
                        pdf_links.append({"url": href, "text": text, "score": 1})
            except Exception:
                pass
    except Exception:
        pass

    if pdf_links:
        pdf_links.sort(key=lambda x: -x["score"])
        return pdf_links
    return None


def main():
    pdf_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "pdfs")
    log_file = "listedcompany_scan_log.txt"

    with session_scope() as s:
        has_docs = set(r[0] for r in s.execute(select(Document.company_id).distinct()).all())
        companies = s.execute(select(Company)).scalars().all()
        missing = [c for c in companies if c.id not in has_docs]

    print(f"Scanning listedcompany.com for {len(missing)} companies")
    found = checked = skipped = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        for i, company in enumerate(missing):
            shortnames = make_shortnames(company)
            result = None

            for shortname in shortnames[:4]:
                result = check_listedcompany(page, shortname)
                if result:
                    break

            if not result:
                status = "NOT_FOUND"
                checked += 1
            else:
                # Try downloading
                company_dir = pdf_dir / company.stock_code
                company_dir.mkdir(parents=True, exist_ok=True)
                downloaded = False

                for candidate in result[:3]:
                    url = candidate["url"]
                    raw = url.rsplit("/", 1)[-1]
                    safe = re.sub(r'[<>:"/\\|?*]', "_", raw)[:120]
                    if not safe.lower().endswith(".pdf"):
                        safe += ".pdf"
                    dest = company_dir / safe

                    if download_pdf(url, dest):
                        if has_financial_statements(dest):
                            with session_scope() as s:
                                ingest_file(s, dest, DocSource.IR, company_id=company.id)
                                s.commit()
                            downloaded = True
                            break
                        else:
                            dest.unlink(missing_ok=True)

                if downloaded:
                    status = "FOUND"
                    found += 1
                else:
                    status = "PDF_NO_MATCH"
                    checked += 1

            line = f"[{i+1}/{len(missing)}] {company.stock_code} {company.name[:25]:25s} -> {status}\n"
            print(line, end="", flush=True)
            with open(log_file, "a") as lf:
                lf.write(line)

            if (i + 1) % 25 == 0:
                summary = f"--- found={found} checked={checked} ---\n"
                print(summary, end="", flush=True)
                with open(log_file, "a") as lf:
                    lf.write(summary)

        browser.close()

    final = f"Done: found={found} checked={checked} total={len(missing)}\n"
    print(final, end="")
    with open(log_file, "a") as f:
        f.write(final)


if __name__ == "__main__":
    main()
