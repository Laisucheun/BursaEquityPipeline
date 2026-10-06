"""Backfill 5 years of annual reports for covered companies.
Uses httpx scraper (via CLI subprocess) with --years 6.
Usage: python backfill_years.py scrape_backfill.txt [pdfs_dir]
"""
import os
import subprocess
import sys


def main():
    list_file = sys.argv[1]
    pdf_dir = sys.argv[2] if len(sys.argv) > 2 else "pdfs"
    log_file = os.path.splitext(list_file)[0] + "_backfill_log.txt"

    with open(list_file) as f:
        codes = [line.strip() for line in f if line.strip()]

    # Resume: skip codes already in log
    done = set()
    if os.path.exists(log_file):
        with open(log_file) as f:
            for line in f:
                if "] " in line and " -> " in line:
                    code = line.split("] ")[1].split(" -> ")[0].strip()
                    done.add(code)

    remaining = [c for c in codes if c not in done]
    msg = f"Backfilling {len(remaining)} companies ({len(done)} already done)\n"
    print(msg, end="")

    found = 0
    not_found = 0
    errors = 0

    for i, code in enumerate(remaining):
        cmd = [
            sys.executable, "-c",
            "from bursa.cli import app; app()",
            "scrape", "annual-reports",
            "--company", code,
            "--years", "6",
            "--pdf-dir", pdf_dir,
        ]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=600, encoding="utf-8", errors="replace",
            )
            output = result.stderr or ""
            found_count = output.count("FOUND") - output.count("NOT_FOUND")
            if found_count > 0:
                found += 1
                status = f"FOUND({found_count})"
            elif "NOT_FOUND" in output:
                not_found += 1
                status = "NOT_FOUND"
            else:
                errors += 1
                status = "ERROR"
        except subprocess.TimeoutExpired:
            errors += 1
            status = "TIMEOUT"
        except Exception:
            errors += 1
            status = "ERR"

        line = f"[{i+1}/{len(remaining)}] {code} -> {status}\n"
        print(line, end="", flush=True)
        with open(log_file, "a") as lf:
            lf.write(line)

        if (i + 1) % 25 == 0:
            summary = f"--- found={found} not_found={not_found} errors={errors} ---\n"
            print(summary, end="", flush=True)
            with open(log_file, "a") as lf:
                lf.write(summary)

    final = f"Done: found={found} not_found={not_found} errors={errors} total={len(remaining)}\n"
    print(final, end="")
    with open(log_file, "a") as f:
        f.write(final)


if __name__ == "__main__":
    main()
