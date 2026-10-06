"""Scrape annual reports one company at a time in separate subprocesses.

This avoids OOM by freeing memory after each company. Already-ingested
PDFs are skipped via SHA dedup in the pipeline.

Usage:
    python scrape_by_sector.py [--sector "Consumer Staples"] [--pdf-dir pdfs]
"""

import argparse
import subprocess
import sys

sys.path.insert(0, "src")

from bursa.db.session import session_scope
from bursa.db.models import Company
from sqlalchemy import select


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sector", help="Limit to this sector")
    parser.add_argument("--pdf-dir", default="pdfs", help="Save PDFs here")
    parser.add_argument("--years", type=int, default=1)
    args = parser.parse_args()

    with session_scope() as s:
        query = select(Company).where(Company.is_watchlist.is_(True)).order_by(Company.stock_code)
        if args.sector:
            query = query.where(Company.sector == args.sector)
        companies = [(c.stock_code, c.name, c.sector) for c in s.scalars(query)]

    print(f"Scraping {len(companies)} companies (sector={args.sector or 'all'}, years={args.years})")

    found = 0
    not_found = 0
    errors = 0

    for i, (code, name, sector) in enumerate(companies):
        cmd = [
            sys.executable, "-c",
            "from bursa.cli import app; app()",
            "scrape", "annual-reports",
            "--company", code,
            "--years", str(args.years),
            "--pdf-dir", args.pdf_dir,
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            output = result.stdout + result.stderr
            if "FOUND" in output and "NOT_FOUND" not in output:
                found += 1
                status = "FOUND"
            elif "NOT_FOUND" in output:
                not_found += 1
                status = "NOT_FOUND"
            elif result.returncode != 0:
                errors += 1
                status = "ERROR"
            else:
                not_found += 1
                status = "NOT_FOUND"
        except subprocess.TimeoutExpired:
            errors += 1
            status = "TIMEOUT"
        except Exception as e:
            errors += 1
            status = f"ERROR: {e}"

        print(f"  [{i+1}/{len(companies)}] {code} {name[:40]:40s} -> {status}")

        if (i + 1) % 25 == 0:
            print(f"  --- Progress: found={found}, not_found={not_found}, errors={errors} ---")

    print(f"\nDone: found={found}, not_found={not_found}, errors={errors}, total={len(companies)}")


if __name__ == "__main__":
    main()
