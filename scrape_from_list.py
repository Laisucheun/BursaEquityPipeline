"""Scrape one company at a time from a stock code list file.
Spawns a fresh subprocess per company to avoid OOM.
Output goes to scrape_log.txt.

Usage: python scrape_from_list.py scrape_information_technology.txt [pdfs]
"""
import os
import subprocess
import sys


def main():
    list_file = sys.argv[1]
    pdf_dir = sys.argv[2] if len(sys.argv) > 2 else "pdfs"
    log_file = os.path.splitext(list_file)[0] + "_log.txt"

    with open(list_file) as f:
        codes = [line.strip() for line in f if line.strip()]

    msg = f"Scraping {len(codes)} companies from {list_file}\n"
    print(msg, end="")
    with open(log_file, "a") as lf:
        lf.write(msg)

    found = 0
    not_found = 0
    errors = 0

    for i, code in enumerate(codes):
        cmd = [
            sys.executable, "-c",
            "from bursa.cli import app; app()",
            "scrape", "annual-reports",
            "--company", code,
            "--years", "1",
            "--pdf-dir", pdf_dir,
        ]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=300, encoding="utf-8", errors="replace",
            )
            output = result.stderr or ""
            if "FOUND" in output and "NOT_FOUND" not in output:
                found += 1
                status = "FOUND"
            elif "NOT_FOUND" in output:
                not_found += 1
                status = "NOT_FOUND"
            else:
                errors += 1
                status = "ERROR"
        except subprocess.TimeoutExpired:
            errors += 1
            status = "TIMEOUT"
        except Exception as e:
            errors += 1
            status = f"ERR"

        line = f"[{i+1}/{len(codes)}] {code} -> {status}\n"
        print(line, end="")
        with open(log_file, "a") as lf:
            lf.write(line)

        if (i + 1) % 25 == 0:
            summary = f"--- found={found} not_found={not_found} errors={errors} ---\n"
            print(summary, end="")
            with open(log_file, "a") as lf:
                lf.write(summary)

    final = f"Done: found={found} not_found={not_found} errors={errors} total={len(codes)}\n"
    print(final, end="")
    with open(log_file, "a") as lf:
        lf.write(final)


if __name__ == "__main__":
    main()
