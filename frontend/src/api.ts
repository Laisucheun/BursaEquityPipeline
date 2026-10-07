// Typed client for the FastAPI backend (src/bursa/api). Types mirror the real
// JSON responses of the route handlers in src/bursa/api/routes/*.py.
// All paths are relative: in dev, Vite proxies /api -> http://127.0.0.1:8000.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { cache: "no-store", ...init });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

const enc = encodeURIComponent;

// ---------------------------------------------------------------- /api/status
export interface Status {
  concepts: number;
  synonyms: number;
  companies: number;
  documents: number;
}

// -------------------------------------------------------------- /api/progress
export type JobStatus = "RUNNING" | "SUCCEEDED" | "FAILED" | "STALLED";

export interface Job {
  id: number;
  kind: string;
  status: JobStatus | string;
  done: number;
  total: number;
  current: string | null;
  error: string | null;
  started_at: string;
  elapsed_s: number;
  eta_s: number | null;
}

export type StatementPrefix = "is" | "bs" | "cf";

/** Benchmark classification counts, e.g. {MATCH, CLOSE, MISMATCH, SCALE_ERROR, MISSING}. */
export type BenchmarkCounts = Partial<Record<string, number>>;

export interface CoverageRow {
  stock_code: string;
  name: string;
  sector: string | null;
  documents: number;
  facts: number;
  years: number[];
  statements: Partial<Record<StatementPrefix, number[]>>;
  validation: { run: number; passed: number };
  benchmark: BenchmarkCounts;
  last_activity: string | null;
}

export interface RecentRun {
  run_id: number;
  at: string | null;
  status: string;
  document: string;
  stock_code: string | null;
  company: string | null;
  facts: number;
}

export interface RoadmapSection {
  section: string;
  items: { done: boolean; text: string }[];
}

export interface Progress {
  generated_at: string;
  totals: { companies: number; documents: number; facts: number; periods: number };
  funnel: { stage: string; count: number }[];
  jobs: Job[];
  document_status: Record<string, number>;
  validation: { run: number; passed: number };
  benchmark: BenchmarkCounts;
  activity: { active: boolean; runs_last_10_min: number; recent: RecentRun[] };
  roadmap: RoadmapSection[];
  companies: CoverageRow[];
  /** Companies whose annual reports must be downloaded by hand (manual_downloads.txt). */
  manual_downloads?: ManualDownload[];
}

export type ManualTier = "LARGE" | "MID" | "SMALL" | "UNKNOWN";

export interface ManualDownload {
  tier: ManualTier | string;
  stock_code: string;
  name: string;
  market_cap_bn: number | null;
  reason: string; // e.g. "unreachable", "no PDF found", "robots blocked", "never tried"
  url: string | null;
  /** The scraper's IR URL looks wrong (e.g. a wa.me link); search manually. */
  url_suspect: boolean;
  documents: number;
  facts: number;
}

// ------------------------------------------------------------- /api/companies
export interface CompanySummary {
  id: number;
  stock_code: string;
  name: string;
  market: string;
  sector: string | null;
  fy_end_month: number | null;
  ir_homepage_url: string | null;
  doc_count: number;
}

export interface CompanyList {
  total: number;
  items: CompanySummary[];
}

export interface SectorCount {
  sector: string;
  count: number;
}

export interface DocumentInfo {
  id: number;
  doc_type: string;
  source: string;
  original_filename: string | null;
  page_count: number | null;
  status: string;
  source_url: string | null;
}

export interface CompanyDetail extends Omit<CompanySummary, "doc_count"> {
  documents: DocumentInfo[];
}

// ----------------------------------------------------------------- /api/facts
export interface CompanyFact {
  concept_key: string;
  value: number | null;
  basis: string;
  confidence: number | null;
}

/** `periods` keys look like "2025 (FY)", "2025 (INSTANT)", "2025 (Q1)". */
export interface CompanyFacts {
  stock_code: string;
  name: string;
  periods: Record<string, CompanyFact[]>;
}

// ------------------------------------------------------------ /api/validation
export interface ValidationRow {
  rule_key: string;
  passed: boolean;
  expected: number | null;
  actual: number | null;
  delta: number | null;
  detail: string | null;
  period_end: string;
  fiscal_year: number;
}

export interface CompanyValidation {
  stock_code: string;
  name: string;
  results: ValidationRow[];
}

// ------------------------------------------------------------- /api/benchmark
export interface BenchmarkRow {
  concept_key: string;
  fiscal_year: number;
  our_value: number | null;
  external_value: number | null;
  deviation_pct: number | null;
  classification: string;
  detail: string | null;
}

export interface CompanyBenchmark {
  stock_code: string;
  name: string;
  results: BenchmarkRow[];
}

// ------------------------------------------------------------- /api/valuation
export interface ValuationYear {
  fiscal_year: number;
  ebit: number | null;
  ebitda: number | null;
  effective_tax_rate: number | null;
  nopat: number | null;
  dep_amort: number | null;
  capex: number | null;
  fcff: number | null;
  fcfe: number | null;
  net_debt: number | null;
}

export interface CompanyValuation {
  stock_code: string;
  name: string;
  years: ValuationYear[];
}

// -------------------------------------------------------------- /api/analysis
export interface DuPont3 {
  fiscal_year: number;
  roe: number;
  net_margin: number;
  asset_turnover: number;
  equity_multiplier: number;
}

export interface DuPont5 {
  fiscal_year: number;
  roe: number;
  tax_burden: number;
  interest_burden: number;
  operating_margin: number;
  asset_turnover: number;
  equity_multiplier: number;
}

export interface DuPontAnalysis {
  stock_code: string;
  name: string;
  three_factor: DuPont3[];
  five_factor: DuPont5[];
}

export interface GrowthMetric {
  label: string;
  years: number;
  start_year: number;
  end_year: number;
  start_value: number;
  end_value: number;
  cagr: number;
}

export interface GrowthAnalysis {
  stock_code: string;
  name: string;
  revenue_cagr_3y: GrowthMetric | null;
  revenue_cagr_5y: GrowthMetric | null;
  earnings_cagr_3y: GrowthMetric | null;
  earnings_cagr_5y: GrowthMetric | null;
  asset_cagr_3y: GrowthMetric | null;
  asset_cagr_5y: GrowthMetric | null;
  roe_trend: { fiscal_year: number; roe: number }[];
}

export interface YearRatios {
  fiscal_year: number;
  period_end: string;
  price: number | null;
  shares: number | null;
  market_cap: number | null;
  eps: number | null;
  book_value: number | null;
  ebitda: number | null;
  net_debt: number | null;
  pe_ratio: number | null;
  pb_ratio: number | null;
  ev_ebitda: number | null;
  dividend_yield: number | null;
}

export interface PriceAnalysis {
  stock_code: string;
  name: string;
  ticker: string;
  current_price: number | null;
  years: YearRatios[];
}

// ---------------------------------------------------------------- /api/upload
export interface UploadResult {
  document_id: number;
  created: boolean;
  page_count: number | null;
  has_financial_statements: boolean;
  statements_found: string[];
  facts_written: number;
  facts_updated: number;
  periods_created: number;
  facts_derived: number;
  validation: {
    rules_run: number;
    rules_passed: number;
    rules_failed: number;
    failures: { rule: string; detail: string | null }[];
  };
  comparative: { match: number; rounding: number; restatement: number } | null;
}

// ------------------------------------------------- /api/review, /api/concepts
// src/bursa/api/routes/review.py
//   GET  /api/review?status=open|resolved|all&reason=&stock_code=&limit=&offset= -> ReviewList
//   POST /api/review/{id}/resolve   body: ReviewResolveRequest -> ReviewItem
//        (422 when map has no/unknown concept_key or the concept's statement differs from the row's)
//   GET  /api/concepts?statement=is|bs|cf -> ConceptOption[]

export interface ReviewResolution {
  action: "map" | "ignore" | "reject";
  concept_key?: string;
  synonym?: { normalized: string | null; company_scoped: boolean };
  note?: string;
}

export interface ReviewItem {
  id: number;
  run_id: number;
  document_id: number;
  document_filename: string | null;
  stock_code: string | null;
  company_name: string | null;
  raw_row_id: number | null;
  fact_id: number | null;
  reason: string; // e.g. "UNMAPPED_LABEL"
  /** Raw JSON string ({label, statement, page_no, values, ...}). */
  detail: string | null;
  severity: number;
  created_at: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution: ReviewResolution | null;
  // Parsed from `detail`.
  raw_label: string | null;
  page_no: number | null;
  statement: StatementPrefix | null;
  cells: { col_index: number; text: string }[] | null;
  /** Mapper suggestions for UNMAPPED_LABEL items, best first. */
  suggestions: { concept_key: string; label: string; score: number }[] | null;
}

export interface ReviewList {
  total: number;
  items: ReviewItem[];
  reasons: { reason: string; count: number }[];
}

export interface ReviewResolveRequest {
  action: "map" | "ignore" | "reject";
  concept_key?: string; // required when action == "map"
  add_synonym?: boolean; // persist label -> concept in concept_synonyms
  company_scoped?: boolean; // synonym only for this issuer
  resolved_by?: string;
}

export interface ConceptOption {
  concept_key: string;
  statement: string;
  label: string;
}

// ------------------------------------------------------------- /api/dividends
// src/bursa/analysis/dividends.py (DividendHistory / YearDividend via asdict)
export interface YearDividend {
  fiscal_year: number;
  period_end: string;
  dividends: number | null; // RM, positive
  dividends_basis: "cash" | "equity" | string | null;
  shares: number | null;
  patami: number | null;
  eps_sen: number | null;
  dps_sen: number | null;
  payout_ratio: number | null; // fraction
  dividend_cover: number | null; // x
  dps_growth: number | null; // fraction
  price: number | null; // RM, only with ?prices=true
  dividend_yield: number | null; // fraction, only with ?prices=true
  /** null = no dividend information this year. */
  paid: boolean | null;
  /** field name -> where the figure came from */
  sources: Record<string, string>;
  flags: string[];
}

export interface DividendHistory {
  stock_code: string;
  name: string;
  is_reit: boolean;
  years: YearDividend[];
  streak: number;
  streak_end: number | null;
  longest_streak: number;
  median_payout: number | null;
  notes: string[];
}

// -------------------------------------------------------------- /api/fiveyear
// src/bursa/api/routes/fiveyear.py
export type FiveYearClass = "MATCH" | "CLOSE" | "MISMATCH" | "SCALE_ERROR" | "ONLY_IN_SUMMARY";

export interface FiveYearCheck {
  fiscal_year: number;
  concept_key: string;
  summary_value: number | null;
  our_value: number | null;
  /** Fraction (0.0007 = 0.07%), unlike /api/benchmark which reports percent. */
  deviation_pct: number | null;
  classification: FiveYearClass | string;
  detail: string;
  page_no: number;
  document_id: number | null;
  restated: boolean;
}

export interface FiveYearResult {
  stock_code: string;
  name: string;
  documents_scanned: number;
  documents_with_summary: number;
  documents: { document_id: number; report_year: number; summary_pages: number[]; error: string | null }[];
  summary_pages: number[];
  counts: Record<FiveYearClass, number>;
  /** MATCH share among comparable checks (fraction), null when nothing comparable. */
  agreement: number | null;
  /** +1/-1 when the summary years line up with our facts one fiscal year off. */
  year_shift: number | null;
  fact_years: number[];
  checks: FiveYearCheck[];
}

// ----------------------------------------------------------------- /api/peers
// src/bursa/analysis/peers.py (PeerComparison via asdict)
export interface PeerSector {
  sector: string;
  companies: number;
  with_facts: number;
}

export interface PeerSnapshot {
  stock_code: string;
  name: string;
  sector: string | null;
  industry: string | null;
  profile: "general" | "bank" | "insurance" | "reit" | string;
  fiscal_year: number | null;
  period_end: string | null;
  metrics: Record<string, number | null>;
  not_applicable: string[];
  /** metric -> why the value was flagged implausible (excluded from stats). */
  flags: Record<string, string>;
  /** metric -> percentile 0..100 within the comparison set. */
  percentiles: Record<string, number | null>;
  warnings: string[];
}

export interface PeerMetricStats {
  metric: string;
  n: number;
  n_flagged: number;
  median: number | null;
  q1: number | null;
  q3: number | null;
  min: number | null;
  max: number | null;
}

export interface PeerComparison {
  title: string;
  sector: string | null;
  requested_fy: number | null;
  rows: PeerSnapshot[];
  stats: Record<string, PeerMetricStats>;
  /** fiscal year -> number of companies whose snapshot is that year (JSON keys are strings). */
  fiscal_years: Record<string, number>;
  missing: string[];
  notes: string[];
}

// ------------------------------------------------------------------ client
export const api = {
  status: () => request<Status>("/api/status"),
  progress: () => request<Progress>("/api/progress"),

  companies: (p: { search?: string; sector?: string; limit?: number; offset?: number } = {}) =>
    request<CompanyList>(`/api/companies${qs(p)}`),
  sectors: () => request<SectorCount[]>("/api/companies/sectors"),
  company: (code: string) => request<CompanyDetail>(`/api/companies/${enc(code)}`),

  // NOTE: /api/facts/{code} returns 200 {"error": "..."} (not 404) for unknown codes.
  facts: async (code: string, fiscalYear?: number) => {
    const r = await request<CompanyFacts | { error: string }>(
      `/api/facts/${enc(code)}${qs({ fiscal_year: fiscalYear })}`,
    );
    if ("error" in r) throw new ApiError(404, r.error);
    return r;
  },
  validation: (code: string) => request<CompanyValidation>(`/api/validation/${enc(code)}`),
  benchmark: (code: string) => request<CompanyBenchmark>(`/api/benchmark/${enc(code)}`),
  valuation: (code: string) => request<CompanyValuation>(`/api/valuation/${enc(code)}`),
  dupont: (code: string) => request<DuPontAnalysis>(`/api/analysis/${enc(code)}/dupont`),
  growth: (code: string) => request<GrowthAnalysis>(`/api/analysis/${enc(code)}/growth`),
  prices: (code: string) => request<PriceAnalysis>(`/api/analysis/${enc(code)}/prices`),

  reviewItems: (p: { status?: string; reason?: string; stock_code?: string; limit?: number; offset?: number } = {}) =>
    request<ReviewList>(`/api/review${qs(p)}`),
  resolveReview: (id: number, body: ReviewResolveRequest) =>
    request<ReviewItem>(`/api/review/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  concepts: (statement?: string) => request<ConceptOption[]>(`/api/concepts${qs({ statement })}`),

  /** prices=true fetches closing prices (network, several seconds) to compute yield. */
  dividends: (code: string, prices = false) =>
    request<DividendHistory>(`/api/dividends/${enc(code)}${qs({ prices: prices ? "true" : "false" })}`),
  /** Parses the annual report PDFs on the fly; can take several seconds. */
  fiveYear: (code: string, years = 1) => request<FiveYearResult>(`/api/fiveyear/${enc(code)}${qs({ years })}`),

  peerSectors: () => request<PeerSector[]>("/api/peers/sectors"),
  sectorPeers: (sector: string, fy?: number) =>
    request<PeerComparison>(`/api/peers/sector/${enc(sector)}${qs({ fy })}`),
  companyPeers: (code: string, peers: string[] = [], fy?: number) => {
    const p = new URLSearchParams();
    for (const c of peers) if (c.trim()) p.append("peers", c.trim());
    if (fy) p.set("fy", String(fy));
    const s = p.toString();
    return request<PeerComparison>(`/api/peers/${enc(code)}${s ? `?${s}` : ""}`);
  },
};

/**
 * POST /api/upload (multipart: file, stock_code). Uses XHR so upload progress
 * can be reported; the server then runs the full pipeline synchronously.
 */
export function uploadPdf(
  file: File,
  stockCode: string,
  onProgress?: (fraction: number) => void,
): { promise: Promise<UploadResult>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<UploadResult>((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    form.append("stock_code", stockCode);
    xhr.open("POST", "/api/upload");
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve(xhr.response as UploadResult);
      else {
        const d = (xhr.response as { detail?: unknown } | null)?.detail;
        reject(new ApiError(xhr.status, typeof d === "string" ? d : d ? JSON.stringify(d) : `HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error - is the backend running?"));
    xhr.onabort = () => reject(new ApiError(0, "Upload cancelled"));
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}
