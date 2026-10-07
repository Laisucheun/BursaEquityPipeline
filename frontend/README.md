# Bursa Equity Pipeline – frontend

React + TypeScript + Vite UI for the FastAPI backend in `src/bursa/api`.

## Run

```powershell
# 1. Backend (from repo root), serves the API on 127.0.0.1:8000
.venv\Scripts\python -m bursa.cli serve

# 2. Frontend
cd frontend
npm install
npm run dev          # http://localhost:3000, proxies /api -> http://127.0.0.1:8000
```

Other scripts: `npm run build` (type-check + production build to `dist/`), `npm run preview` (serve `dist/` on :3000 with the same proxy), `npm run typecheck`.

## Pages

| Route | What it shows |
|---|---|
| `/` | Dashboard (port of `src/bursa/api/static/dashboard.html`): KPIs, funnel, live jobs (polls `/api/progress` every 2s while a job is RUNNING, otherwise every 10s), coverage table with search and sort, manual-downloads checklist (tier filter, pending-only toggle, search, IR link only for non-suspect http(s) URLs), recent runs, roadmap |
| `/companies` | Company list with search, sector filter, and pagination (`/api/companies`) |
| `/company/:code` | Tabs: Documents, Facts (statement × fiscal year, annual or quarterly, by basis), Validation, Benchmark, Valuation, DuPont, Growth, Prices, Dividends, 5-year check, Peers. Tabs load on first open and keep their data. Prices calls Yahoo Finance and loads only on click. Dividends shows streak, payout, an inline-SVG DPS chart, and per-year flags and sources; "Load yield" re-fetches with `prices=true`. The 5-year check runs only on click because it parses PDFs (a few seconds). It shows agreement, year shift, and a colour-coded concept × year grid. Peers shows sector peers by default, or a custom peer list and FY. |
| `/peers` | Sector picker, then a table of that sector's companies with metrics, upper/median/lower quartile rows, percentile colouring (direction-aware), implausible values flagged, and notes. `?sector=&fy=` are kept in the URL. |
| `/upload` | PDF upload to `POST /api/upload` with upload progress and the pipeline result |
| `/review` | Concept review queue: filter by status, reason and stock code. Each item can be mapped to a concept (suggestion chips and a concept picker, optionally saved as a synonym for all companies or one company), ignored, or rejected. Items are raised during `normalize`. When the queue is empty, "Show a sample item" previews the workflow with a mocked resolver and sends nothing. |

## Endpoints used

| Endpoint | Used by |
|---|---|
| `GET /api/progress` (incl. `manual_downloads[]`) | Dashboard |
| `GET /api/companies`, `/api/companies/sectors`, `/api/companies/{code}` | Companies, company detail |
| `GET /api/facts/{code}`, `/api/validation/{code}`, `/api/benchmark/{code}`, `/api/valuation/{code}` | company detail |
| `GET /api/analysis/{code}/dupont\|growth\|prices` | company detail |
| `GET /api/dividends/{code}?prices=false\|true` | Dividends tab (`prices=true` adds price and yield; slow, network) |
| `GET /api/fiveyear/{code}?years=N` | 5-year check tab (`years=0` = all reports). `deviation_pct` here is a **fraction**, while `/api/benchmark` reports percent. |
| `GET /api/peers/sectors`, `/api/peers/sector/{sector}?fy=`, `/api/peers/{code}?peers=A&peers=B&fy=` | Peers page and tab. `percentiles` are 0–100 and `flags` maps metric → reason. |
| `GET /api/review?status=&reason=&stock_code=&limit=&offset=` | Review |
| `POST /api/review/{id}/resolve` `{action, concept_key?, add_synonym?, company_scoped?, resolved_by?}` | Review. Returns 422 when `map` is missing a concept or the concept's statement differs from the row's. |
| `GET /api/concepts?statement=is\|bs\|cf` | Review concept picker |
| `POST /api/upload` | Upload |

## Layout

- `src/api.ts`: typed API client. Response types mirror `src/bursa/api/routes/*.py`.
- `src/lib.ts`: formatters and the `useAsync` / `useDebounced` hooks.
- `src/components.tsx`: shared bits (progress bar, coverage cells, an inline-SVG line chart).
- `src/peers.tsx`: the peer comparison table, shared by `/peers` and the company Peers tab.
- `src/pages/CompanyTabs.tsx`: Dividends, 5-year check, and Peers tabs.
- `src/pages/*`: one file per route.
- `src/styles.css`: light and dark themes via `prefers-color-scheme`.
