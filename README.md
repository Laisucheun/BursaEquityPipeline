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
bursa serve            # start API server at http://localhost:8000
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `bursa init-db` | Create all database tables and seed taxonomy |
| `bursa upload <pdf> -c <CODE>` | Upload a PDF and run the full pipeline |
| `bursa ingest [path]` | Ingest PDFs from the inbox or a path |
| `bursa serve` | Start the FastAPI server |
| `bursa status` | Pipeline health at a glance |
| `bursa company add/list/set-ir-url/import-csv` | Manage companies |
| `bursa scrape annual-reports` | Crawl IR sites for annual reports |
| `bursa scrape discover-ir-urls` | Use Claude + web search to find IR URLs |
| `bursa extract statements` | Extract IS, BS, CF, EQ from documents |
| `bursa normalize facts` | Write Fact rows from extractions |
| `bursa normalize derive` | Derive missing facts from identities |
| `bursa validate facts` | Run accounting-identity validation |
| `bursa validate comparative` | Cross-document consistency check |
| `bursa benchmark facts` | Cross-validate against yfinance |
| `bursa valuation metrics` | Compute FCFF, FCFE, EBITDA, EV |

## API

Start with `bursa serve`, docs at `http://localhost:8000/docs`.

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | Pipeline health |
| `GET /api/companies` | List/search companies |
| `GET /api/companies/{code}` | Company detail with documents |
| `POST /api/upload` | Upload PDF, run pipeline |
| `GET /api/facts/{code}` | Facts by company/period |
| `GET /api/validation/{code}` | Validation results |
| `GET /api/benchmark/{code}` | Benchmark results |
| `GET /api/valuation/{code}` | Valuation metrics |

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
  scrapers/          IR website crawler, content filter, robots.txt compliance
  cli.py             Typer CLI
tests/               289 tests
```

## Tests

```powershell
.venv\Scripts\python -m pytest -q     # 289 tests
```

## Roadmap

### Data Completeness

- [ ] Process 385 manual-download companies (3 large + 24 mid + 358 small) — see `manual_downloads.txt`
- [ ] Add quarterly report ingestion (Q1–Q4 period semantics, cumulative vs individual quarter)
- [ ] Historical data before 2021

### Infrastructure

- [ ] Alembic migrations for schema evolution
- [ ] Switch to R2 cloud storage for team access (backend exists in `src/bursa/storage/r2.py`)
- [ ] Export to Excel/CSV for analyst consumption
- [ ] React frontend (dashboard, company detail, upload UI, concept review UI)

### Extraction Quality

- [ ] OCR support for scanned PDFs (Tesseract + pdf2image)
- [ ] Wire LLM concept mapper for unmapped rows (exists in `src/bursa/mapping/llm_mapper.py`)
- [ ] Equity statement matrix layout → structured facts normalization

### Validation & Analysis

- [ ] Bursa financial highlights cross-check
- [ ] 5-year summary page extraction and cross-validation
- [ ] EPS back-check against shares outstanding
- [ ] Share price integration (P/E, P/B, EV/EBITDA with market data)
- [ ] Dividend history and yield tracking
- [ ] Growth rate computation (revenue CAGR, earnings growth)
- [ ] DuPont decomposition (3-factor and 5-factor ROE)
- [ ] Peer comparison / sector analysis

## Legal

`bursamalaysia.com` is never targeted — it sits behind a Cloudflare CAPTCHA. For IR sites:
`robots.txt` is checked and respected per host at runtime. Annual report PDFs are
copyrighted: keep originals private. Extracted numeric facts are facts; the source PDFs are
not yours to redistribute.
