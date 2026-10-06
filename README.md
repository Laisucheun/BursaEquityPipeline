  # Bursa Equity Pipeline

Turns Bursa Malaysia annual and quarterly report PDFs into a queryable database of
financial statement line items.

## The design bet

Bursa filings are **visually inconsistent but semantically standardised** — every issuer
reports under MFRS (= IFRS), and quarterlies follow Listing Requirements Appendix 9B, but
the tables come in every imaginable combination of borders, shading, fonts and column
order, and older ones are scans.

So the pipeline does not parse layouts. It:

1. **Extracts cells with coordinates** deterministically. Figures are right-aligned in
   every filing, so clustering the *right edges* of numeric words recovers the columns
   regardless of styling. (`src/bursa/extract/layout.py`)
2. **Asks an LLM only for meaning** — which canonical concept each row label is, and which
   period each column represents. The model is never shown a number and never asked to
   produce one, so it cannot hallucinate a figure into the database. `assert_no_figures`
   enforces this at runtime, not by convention. (`src/bursa/mapping/llm_mapper.py`)
3. **Validates against accounting identities.** Assets = liabilities + equity, the cash
   flow rolls forward, EPS × shares ≈ PATAMI, and — the high-value one — a prior-year
   comparative column must agree with what was originally filed a year earlier.
   (`src/bursa/validate/rules.py`)
4. **Sends everything doubtful to a human**, whose correction is stored as a synonym, so
   the same label from the same issuer is resolved for free next quarter.

## Quick start

```powershell
uv sync --extra dev
Copy-Item .env.example .env
uv run bursa init-db                      # schema + 92 concepts + 313 seed synonyms
uv run bursa company add 5285 "Some Bhd" --fy-end-month 12
# drop PDFs into ./inbox, then:
uv run bursa ingest
uv run bursa status
```

Default database is a local SQLite file, so there is nothing to install. The schema is
Postgres-compatible; switching is one line in `.env`:

```
DATABASE_URL=postgresql+psycopg://bursa:bursa@localhost:5432/bursa
```
followed by `uv sync --extra postgres`.

## Why a fact is keyed by the document that reported it

`facts` is unique on
`(company, concept, period, basis, continuity, reported_in_document_id)`.

That last column is the important one. FY2023 as originally filed and FY2023 as restated in
the FY2024 report are **two different true facts**. Keying on the reporting document means
restatements, prior-period comparatives and re-extractions coexist instead of colliding.
`facts_current` picks the most recently filed value per period; the difference between the
two *is* the restatement report.

## The five traps this is built around

| Trap | Handled by |
|---|---|
| Column semantics (current quarter vs preceding-year vs cumulative) | `StatementTable.column_map`, assigned explicitly and reviewed |
| Scale and sign (`RM'000`, `(1,234)`, `–`) | `normalize/scale.py`, `normalize/numbers.py` — per-share figures are never rescaled |
| Label drift ("Revenue"/"Turnover"/"Hasil") | `concept_synonyms`, statement-scoped, grown by review |
| Restatements | `reported_in_document_id` on every fact |
| Scanned PDFs | `DocumentClassification.needs_ocr` routes to Docling/Textract |

## Layout

```
src/bursa/
  config.py            settings
  db/                  models, enums, session
  storage/             local + Cloudflare R2 behind one interface
  extract/
    classify.py        which pages carry the primary statements
    layout.py          cells + bounding boxes from a statement page
  mapping/
    taxonomy.py        92 canonical concepts, MFRS-shaped, with seed synonyms
    synonyms.py        label normalisation + the deterministic pre-pass
    llm_mapper.py      the LLM classifier (labels in, concepts out)
  normalize/           numbers, scale, fiscal calendar
  validate/rules.py    accounting identities
  pipeline/            stages as plain functions
  cli.py
tests/
  fixtures/synthetic.py   Bursa-style PDFs, incl. a deliberately ugly variant
```

Every pipeline stage is a plain function taking a `Session`. Prefect (optional extra) only
orchestrates them, so everything is runnable and testable without an orchestrator.

## Automated acquisition (`src/bursa/scrapers/`)

**`bursamalaysia.com` sits behind a Cloudflare Turnstile "verify you are human" challenge on
every path checked, including `robots.txt` itself** — confirmed by direct browser navigation.
Automating past a CAPTCHA isn't something this codebase does, so there is no scraper against
Bursa's own site. Company **investor-relations (IR) websites** are the only automated source.

```powershell
uv sync --extra scraping                      # beautifulsoup4 + lxml
bursa company set-ir-url 5347 "https://www.tnb.com.my"
bursa scrape annual-reports --company 5347 --dry-run   # resolve without downloading
bursa scrape annual-reports --company 5347             # download + ingest
bursa scrape report                                    # found/missed/error, per company
```

The IR sniffer (`ir_fallback.py`) is grounded in real sites, re-verified against the live
pages (not just fixtures) after every fix — a discipline that mattered, since three fixes in a
row each passed the fixture and still failed live:

- **tnb.com.my** — the easy case: a flat "one link per year" hub, link text says "Annual Report
  for 2024" outright.
- **publicbankgroup.com** — the hard case. A year's report, its AGM notice, administrative
  details, and proxy form all sit in **one shared list**, labelled by a plain styled `<div>`
  (not a semantic heading) that sits as a sibling *before* the list, never a wrapping ancestor
  of it. Getting this right took two false starts: an unbounded "nearest matching heading
  anywhere earlier in the document" search misclassified an unrelated PDF on a different site
  (fixed by bounding the label lookup to one *direct* sibling per level, never a document-wide
  scan); then a character-length cap on "is this container too broad to trust" let a
  short-but-many-rows list (275 characters, six unrelated documents) through as one document's
  text — fixed by detecting a genuine multi-row container **structurally**
  (`_is_multi_row_container`: does it wrap several siblings that each have their own link?),
  since aggregate text length turned out not to correlate with that at all.
- **ihhhealthcare.com** — the honest-miss case: its real report list is rendered client-side,
  so a plain HTTP fetch sees zero `.pdf` hrefs. `NOT_FOUND` here is correct, not a bug — and
  it's also where the unbounded-search false start above was first caught, inventing a match
  from an unrelated press-release PDF via a nearby nav link's text.

Politeness (`http_client.py`, `robots.py`): per-host `robots.txt` compliance, checked and
cached before the first request to any host; per-host rate limiting; bounded global
concurrency; retry with backoff. A company with no `ir_homepage_url` set, or whose site
disallows crawling, is logged as skipped in `scrape_attempts` — never silently dropped, never
overridden.

**Verified against real sites, end to end** (2025-09-29): `bursa scrape annual-reports
--company 5347` downloaded Tenaga Nasional Berhad's real 494-page, 14MB 2025 annual report,
and `classify_document`/`extract_page` ran against it without crashing — it also surfaced a
real tuning gap worth knowing about before relying on classification at scale: large annual
reports have financial-commentary pages (5-year highlights, MD&A) that look statement-like
enough to be misclassified alongside the actual primary statements. Across a real, verified
20-company watchlist (stock codes and IR URLs cross-checked live before being trusted — one
LLM-proposed URL, labelled "high confidence," pointed a real company at a stranger's personal
domain), a dry run resolves **11 of 20** — the misses are timeouts, WAF blocks, or genuinely
JS-rendered content, all visible in `bursa scrape report`, none silent.

### Scraper status and next steps (2026-10-01)

Watchlist: 96 companies, 93 ingested documents, 25 still at zero after a full
search-and-verify pass (`WebSearch` + direct download, each candidate confirmed with
`pymupdf`/`has_financial_statements` before ingesting — never trust a search snippet, and
never trust `WebFetch`'s own PDF reading, which has been wrong on real, valid PDFs).

Three independent recall strategies were tried and fully exhausted for the remaining
zero-doc companies, each producing real evidence rather than being abandoned on a guess:
smarter subpath guessing, retrying from each company's bare root domain, and increasing
`ir_fallback.MAX_HOPS` (2 → 3, kept). None recovered anything — the bottleneck for this
batch is link *discovery* (JS-rendered report lists, or wording the scraper's `NAV_PATTERN`
doesn't match), not crawl depth or URL guessing.

**Still open, by category:**
- **Bot-protected official sites** (need a real browser, like the Frontken/Tropicana
  insage.com.my downloads) — Allianz Malaysia (Cloudflare), Top Glove (Akamai).
- **Broken or moved IR platforms** — KLCC Property Holdings (`klccp.listedcompany.com` no
  longer resolves), IHH Healthcare (`ihhmy.listedcompany.com` template-errors).
- **No usable lead found yet** — Sunway Berhad, Ranhill Utilities, Lingkaran Trans Kota,
  Axis REIT, MY E.G. Services, AirAsia Group, AirAsia X, Al-Aqar Healthcare REIT (x2),
  Pentamaster/Pentastar, Maybank, Nestlé, Telekom Malaysia, Sunway Construction.
- **Interactive-viewer platforms** (insage.com.my FlippingBook, Maxis's own microsite) hold
  real PDFs behind a JS-generated, non-static download link — no static scrape will ever find
  these; only a browser-driven click-through works. Two done manually (Frontken, Tropicana);
  the same gap blocks Maxis.

**Stock-code data-quality bugs found in the watchlist this session** (root cause: the messy
claude.ai-assisted import earlier in the project) — fixed in place: Chin Hin Group Berhad
(`1919` → real code `5273`), Glomac Berhad (`5304` → real code `5020`), AirAsia Group's
IR URL (was pointing at AirAsia X's site). **Not yet fixed**: Supermax Corporation Berhad's
real Bursa code is `7106`; the watchlist has it split across two wrong codes, `5151` (zero
docs) and `5283` (1 verified document) — needs the same careful fix (confirm nothing else
already claims `7106`, then migrate). Also still only *suspected*, not hash-confirmed:
`5114`/`5116` (both "Al-Aqar Healthcare REIT") and `5211`/`5379` (both "Sunway Berhad") may be
the same duplicate-code pattern.

## Tests

```powershell
uv run pytest -q          # 152 tests
uv run ruff check .
```

The test that states the core extraction thesis is
`tests/test_layout.py::test_styling_does_not_change_the_numbers`: the same figures rendered
with different fonts, shaded rows, shifted columns and wrapped labels must extract
identically.

## Status

Built: schema, taxonomy, ingest with content-hash dedupe, page classification, layout
extraction, the LLM mapper with its no-figures guard, the validation rules, and the IR-site
scraper.

Not yet built: writing facts to the database (`normalize` → `facts`), the review UI, and the
FastAPI/Next.js app. See `~/.claude/plans/im-thinking-of-creating-floating-moore.md` for the
scraper's design notes and milestones.

## Legal

`bursamalaysia.com` is never targeted (see above). For IR sites: `robots.txt` is checked and
respected per host at runtime, not just once by hand. Annual report PDFs are copyrighted: keep
originals private. Extracted numeric facts are facts; the source PDFs are not yours to
redistribute.
