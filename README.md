# Bursa Equity Pipeline

Turns Bursa Malaysia annual report PDFs into a queryable database of financial
statement line items — deterministic extraction, no LLM needed for the core path.

## The design bet

Bursa filings are **visually inconsistent but semantically standardised** — every issuer
reports under MFRS (= IFRS), but the tables come in every imaginable combination of
borders, shading, fonts and column order.

The pipeline does not parse layouts by style. It:

1. **Extracts cells with coordinates** deterministically. Figures are right-aligned in
   every filing, so clustering the *right edges* of numeric words recovers the columns
   regardless of styling. (`src/bursa/extract/layout.py`)
2. **Maps labels to concepts** via a seeded synonym table (~500 synonyms across 137
   concepts, English + Malay, including bank and REIT-specific vocabulary). The LLM mapper
   (`src/bursa/mapping/llm_mapper.py`) exists for unmapped rows but the deterministic path
   handles the vast majority. The model is never shown a number and never asked to produce
   one — `assert_no_figures` enforces this at runtime.
3. **Validates against accounting identities.** 11 rules: assets = liabilities + equity,
   cash flow rolls forward, EPS × shares ≈ PATAMI, plus magnitude sanity checks and
   cross-document comparative matching.
4. **Cross-validates against yfinance** — 6 key concepts benchmarked against Yahoo Finance
   data, classified as MATCH/CLOSE/MISMATCH/SCALE_ERROR.

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"

bursa init-db          # schema + 137 concepts + ~500 seed synonyms
bursa upload report.pdf --company 1295   # full pipeline in one shot
bursa serve            # API + progress dashboard at http://localhost:8000
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `bursa init-db` | Create all database tables and seed taxonomy |
| `bursa upload <pdf> -c <CODE>` | Upload a PDF and run the full pipeline |
| `bursa ingest [path]` | Ingest PDFs from the inbox or a path |
| `bursa serve` | Start the FastAPI server and progress dashboard |
| `bursa status` | Pipeline health at a glance |
| `bursa company add/list/set-ir-url/import-csv` | Manage companies |
| `bursa scrape annual-reports` | Crawl IR sites for annual reports |
| `bursa scrape discover-ir-urls` | Use Claude + web search to find IR URLs |
| `bursa extract statements` | Extract IS, BS, CF, EQ from documents |
| `bursa normalize facts` | Write Fact rows from extractions. PDF extraction runs in `--workers` processes (default: CPUs − 2, ~7x faster on 12 threads); one process writes, a commit per company. `--only-without-facts` skips companies already done |
| `bursa normalize derive` | Derive missing facts from identities |
| `bursa validate facts` | Run accounting-identity validation |
| `bursa validate comparative` | Cross-document consistency check |
| `bursa benchmark facts` | Cross-validate against yfinance |
| `bursa valuation metrics` | Compute FCFF, FCFE, EBITDA, EV |
| `bursa analysis dupont` | 3- and 5-factor DuPont ROE decomposition |
| `bursa analysis growth` | Revenue / earnings / asset CAGR (3Y, 5Y), ROE trend |
| `bursa analysis prices` | P/E, P/B, EV/EBITDA, dividend yield at FY-end close (yfinance) |
| `bursa peers sector NAME` / `peers CODE` | Sector medians, quartiles, percentile ranks |
| `bursa export excel -c CODE` / `export csv -o FILE` | Analyst workbook per company / long CSV (`pip install .[export]`) |
| `bursa db upgrade` / `db stamp` / `db check` | Alembic schema migrations |
| `bursa storage migrate-to-r2` / `storage verify` | Copy local PDFs to R2 (dry run unless `--execute`) |

## API

Start with `bursa serve`. The progress dashboard is at `http://localhost:8000/` (pipeline
funnel, per-company coverage by fiscal year, validation/benchmark rates, live job progress for
`bursa normalize facts`, roadmap status); API docs at `http://localhost:8000/docs`.

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Pipeline health |
| `GET /api/progress` | Dashboard snapshot: funnel, coverage, jobs, roadmap |
| `GET /api/peers/sectors`, `/api/peers/sector/{sector}`, `/api/peers/{code}` | Peer comparison |

React frontend (dashboard, companies, company detail, upload): `cd frontend; npm install; npm run dev`
→ http://localhost:3000 (proxies `/api` to the backend on :8000). See `frontend/README.md`.
| `GET /api/companies` | List/search companies |
| `GET /api/companies/{code}` | Company detail with documents |
| `POST /api/upload` | Upload PDF, run pipeline |
| `GET /api/facts/{code}` | Facts by company/period |
| `GET /api/validation/{code}` | Validation results |
| `GET /api/benchmark/{code}` | Benchmark results |
| `GET /api/valuation/{code}` | Valuation metrics |
| `GET /api/analysis/{code}/dupont` | DuPont decomposition |
| `GET /api/analysis/{code}/growth` | Growth rates and ROE trend |
| `GET /api/analysis/{code}/prices` | Market-price ratios |

## Pipeline stages

```
scrape → ingest → extract → normalize → derive → validate → benchmark → valuation
```

## Coverage

- 977 companies registered, 592 with documents
- 82.4% by market cap (RM1,599B / RM1,940B), 92.5% of large caps
- 5-year backfill (2021–2026)
- 137 concepts across IS, BS, CF, EQ (including bank and REIT-specific)

## Why a fact is keyed by the document that reported it

`facts` is unique on
`(company, concept, period, basis, continuity, reported_in_document_id)`.

FY2023 as originally filed and FY2023 as restated in the FY2024 report are **two
different true facts**. Keying on the reporting document means restatements, prior-period
comparatives and re-extractions coexist instead of colliding.

## Layout

```
src/bursa/
  api/               FastAPI backend (companies, upload, facts, validation, benchmark, valuation)
  config.py          settings
  db/                models, enums, session
  storage/           local + Cloudflare R2 behind one interface
  extract/
    classify.py      page classification (IS, BS, CF, EQ headings)
    layout.py        cells + bounding boxes via right-edge clustering
    page_scoring.py  two-layer page selection (keywords + numeric density)
    statement_extract.py  full extraction orchestration
  mapping/
    taxonomy.py      137 canonical concepts with seed synonyms
    synonyms.py      label normalisation + deterministic synonym lookup
    llm_mapper.py    LLM classifier (labels in, concepts out, never sees figures)
  normalize/         numbers, scale, fiscal calendar
  validate/
    rules.py         11 accounting-identity rules
    comparative.py   cross-document consistency (restatement detection)
  pipeline/          ingest, normalize, derive, validate stages
  benchmark/         yfinance cross-validation
  valuation/         FCFF, FCFE, EBITDA, EV computation
  analysis/          annual fact selection, DuPont, growth, market-price ratios
  scrapers/          IR website crawler, content filter, robots.txt compliance
  cli.py             Typer CLI
tests/               336 tests
```

## Tests

```powershell
.venv\Scripts\python -m pytest -q     # 336 tests
```

## Roadmap

### Data Completeness

- [ ] Process 385 manual-download companies (3 large + 24 mid + 358 small) — see `manual_downloads.txt`
- [ ] Add quarterly report ingestion (Q1–Q4 period semantics, cumulative vs individual quarter)
- [ ] Historical data before 2021

### Infrastructure

- [x] Alembic migrations for schema evolution (`bursa db ...`; existing DBs: `bursa db stamp`)
- [ ] Switch to R2 cloud storage for team access — ready and tested offline (`bursa storage migrate-to-r2`, dry run by default); needs R2 credentials to flip
- [x] Export to Excel/CSV for analyst consumption
- [ ] React frontend (dashboard, company detail, upload UI, concept review UI) — in `frontend/`; concept review needs a `/api/review` backend endpoint

### Extraction Quality

- [ ] OCR support for scanned PDFs (Tesseract + pdf2image) — `--ocr` flag wired, not yet verified on a real scanned PDF
- [ ] Wire LLM concept mapper for unmapped rows (exists in `src/bursa/mapping/llm_mapper.py`)
- [ ] Equity statement matrix layout → structured facts normalization

### Validation & Analysis

- [ ] Bursa financial highlights cross-check
- [ ] 5-year summary page extraction and cross-validation
- [ ] EPS back-check against shares outstanding
- [x] Share price integration (P/E, P/B, EV/EBITDA with market data)
- [ ] Dividend history and yield tracking (FY-level cash yield done; per-share history not yet)
- [x] Growth rate computation (revenue CAGR, earnings growth)
- [x] DuPont decomposition (3-factor and 5-factor ROE)
- [x] Peer comparison / sector analysis

## Legal

`bursamalaysia.com` is never targeted — it sits behind a Cloudflare CAPTCHA. For IR sites:
`robots.txt` is checked and respected per host at runtime. Annual report PDFs are
copyrighted: keep originals private. Extracted numeric facts are facts; the source PDFs are
not yours to redistribute.
