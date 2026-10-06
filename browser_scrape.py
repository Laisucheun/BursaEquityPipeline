"""Batch browser scraper — subprocess per company to avoid OOM.
Usage: python browser_scrape.py scrape_list.txt [pdfs_dir]
"""
import subprocess
import sys
from pathlib import Path

def main():
    list_file = sys.argv[1]
    pdf_dir = sys.argv[2] if len(sys.argv) > 2 else "pdfs"
    log_file = Path(list_file).stem + "_browser_log.txt"

    with open(list_file) as f:
        codes = [line.strip() for line in f if line.strip()]

    # Skip already-done codes
    done = set()
    if Path(log_file).exists():
        with open(log_file) as f:
            for line in f:
                if "] " in line and " -> " in line:
                    code = line.split("] ")[1].split(" -> ")[0].strip()
                    done.add(code)

    remaining = [c for c in codes if c not in done]
    print(f"Browser scraping {len(remaining)} companies ({len(done)} already done)")
    found = not_found = errors = 0

    for i, code in enumerate(remaining):
        try:
            result = subprocess.run(
                [sys.executable, "browser_scrape_one.py", code, pdf_dir],
                capture_output=True, timeout=120, encoding="utf-8", errors="replace",
            )
            output = result.stdout.strip()
            if output.startswith("FOUND"):
                status = "FOUND"
                found += 1
            elif output.startswith("NOT_FOUND"):
                status = "NOT_FOUND"
                not_found += 1
            elif output.startswith("TIMEOUT"):
                status = "TIMEOUT"
                errors += 1
            elif output.startswith("SKIP") or output.startswith("NO_URL"):
                status = output.split()[0]
            else:
                status = f"ERROR({output[:60]})"
                errors += 1
        except subprocess.TimeoutExpired:
            status = "SUBPROCESS_TIMEOUT"
            errors += 1
        except Exception as e:
            status = f"ERROR({e})"
            errors += 1

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
