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
| `/` | Dashboard (port of `src/bursa/api/static/dashboard.html`): KPIs, funnel, live jobs (polls `/api/progress` every 2s while a job is RUNNING, otherwise every 10s), coverage table with search and sort, recent runs, roadmap |
| `/companies` | Company list with search, sector filter, and pagination (`/api/companies`) |
| `/company/:code` | Documents, facts (statement × fiscal year, annual or quarterly, by basis), validation, benchmark, valuation, DuPont, and growth. Tabs load on first open. The Prices tab calls Yahoo Finance, so it loads only when you click it. |
| `/upload` | PDF upload to `POST /api/upload` with upload progress and the pipeline result |
| `/review` | Concept review queue. **Needs a backend endpoint that doesn't exist yet** (see below). |

## Concept review endpoint (proposed)

The page is built against this contract (types in `src/api.ts`). Until the backend implements it, the page shows an "endpoint not available yet" notice.

- `GET /api/review?status=open|resolved|all&reason=&stock_code=&limit=&offset=` → `{ total, items: ReviewItem[], reasons: [{reason, count}] }`. Each item has the `review_items` columns plus joined `document_filename`, `stock_code`, `company_name`, `raw_label`, `page_no`, `statement`, `cells`, and optional `suggestions`.
- `POST /api/review/{id}/resolve` with body `{ action: "map"|"ignore"|"reject", concept_key?, add_synonym?, company_scoped?, resolved_by? }` → the updated item. For `map` with `add_synonym`, it also writes a `concept_synonyms` row.
- `GET /api/concepts?statement=is|bs|cf` → `[{ concept_key, statement, label }]`

## Layout

- `src/api.ts`: typed API client. Response types mirror `src/bursa/api/routes/*.py`.
- `src/lib.ts`: formatters and the `useAsync` / `useDebounced` hooks.
- `src/components.tsx`: shared bits (progress bar, coverage cells, an inline-SVG line chart).
- `src/pages/*`: one file per route.
- `src/styles.css`: light and dark themes via `prefers-color-scheme`.
