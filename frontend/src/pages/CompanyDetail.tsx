import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  api,
  type CompanyFacts,
  type DocumentInfo,
  type GrowthMetric,
  type ValidationRow,
  type ValuationYear,
} from "../api";
import { ErrorBox, LineChart, Loading, Section } from "../components";
import { money, num, percent, ratio, useAsync } from "../lib";

const TABS = [
  ["documents", "Documents"],
  ["facts", "Facts"],
  ["validation", "Validation"],
  ["benchmark", "Benchmark"],
  ["valuation", "Valuation"],
  ["dupont", "DuPont"],
  ["growth", "Growth"],
  ["prices", "Prices"],
] as const;
type Tab = (typeof TABS)[number][0];

export default function CompanyDetail() {
  const { code = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = (TABS.find(([k]) => k === params.get("tab"))?.[0] ?? "documents") as Tab;
  // Tabs fetch on first open and keep their data afterwards.
  const [visited, setVisited] = useState<Set<Tab>>(() => new Set([tab]));
  const open = (t: Tab) => {
    setVisited((v) => new Set(v).add(t));
    setParams(t === "documents" ? {} : { tab: t }, { replace: true });
  };
  const seen = (t: Tab) => visited.has(t) || tab === t;

  const company = useAsync(() => api.company(code), [code]);
  const facts = useAsync(() => api.facts(code), [code], seen("facts"));
  const validation = useAsync(() => api.validation(code), [code], seen("validation"));
  const benchmark = useAsync(() => api.benchmark(code), [code], seen("benchmark"));
  const valuation = useAsync(() => api.valuation(code), [code], seen("valuation"));
  const dupont = useAsync(() => api.dupont(code), [code], seen("dupont"));
  const growth = useAsync(() => api.growth(code), [code], seen("growth"));
  // Prices hit Yahoo Finance (slow): only on explicit request.
  const [wantPrices, setWantPrices] = useState(false);
  const prices = useAsync(() => api.prices(code), [code], wantPrices);

  if (company.error) {
    return (
      <div className="panel">
        <ErrorBox error={company.error} />
        <p><Link to="/companies">Back to companies</Link></p>
      </div>
    );
  }
  const c = company.data;

  return (
    <>
      <div className="pagehead">
        <h1>{c ? `${c.name}` : code} <span className="muted">{code}</span></h1>
        <Link to="/companies" className="muted">All companies</Link>
      </div>
      {c && (
        <div className="panel" style={{ marginTop: 16 }}>
          <dl className="dl">
            <dt>Market</dt><dd>{c.market}</dd>
            <dt>Sector</dt><dd>{c.sector ?? "–"}</dd>
            <dt>FY end month</dt><dd>{c.fy_end_month ?? "–"}</dd>
            <dt>IR homepage</dt>
            <dd>{c.ir_homepage_url ? <a href={c.ir_homepage_url} target="_blank" rel="noreferrer">{c.ir_homepage_url}</a> : "–"}</dd>
          </dl>
        </div>
      )}

      <div className="tabs" role="tablist">
        {TABS.map(([k, l]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "on" : ""} onClick={() => open(k)}>
            {l}{k === "documents" && c ? ` (${c.documents.length})` : ""}
          </button>
        ))}
      </div>

      <div className="tabpanel panel">
        {tab === "documents" && (c ? <Documents docs={c.documents} /> : <Loading />)}
        {tab === "facts" && (
          <Section state={facts} isEmpty={(d) => !Object.keys(d.periods).length} empty="No facts extracted yet.">
            {(d) => <FactsTable data={d} />}
          </Section>
        )}
        {tab === "validation" && (
          <Section state={validation} isEmpty={(d) => !d.results.length} empty="No validation results yet.">
            {(d) => <ValidationTable rows={d.results} />}
          </Section>
        )}
        {tab === "benchmark" && (
          <Section state={benchmark} isEmpty={(d) => !d.results.length} empty="Not benchmarked yet (run `bursa benchmark`).">
            {(d) => (
              <div className="tablewrap">
                <table>
                  <thead><tr><th>FY</th><th>Concept</th><th className="num">Ours</th><th className="num">External</th>
                    <th className="num">Deviation</th><th>Class</th><th>Detail</th></tr></thead>
                  <tbody>
                    {d.results.map((r) => (
                      <tr key={`${r.fiscal_year}-${r.concept_key}`}>
                        <td>{r.fiscal_year}</td><td><code>{r.concept_key}</code></td>
                        <td className="num">{money(r.our_value)}</td><td className="num">{money(r.external_value)}</td>
                        <td className="num">{r.deviation_pct == null ? "–" : `${r.deviation_pct.toFixed(2)}%`}</td>
                        <td><span className={`pill ${r.classification}`}>{r.classification}</span></td>
                        <td className="muted wrap-text">{r.detail ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Section>
        )}
        {tab === "valuation" && (
          <Section state={valuation} isEmpty={(d) => !d.years.length} empty="Not enough facts to compute valuation metrics.">
            {(d) => <ValuationTable years={d.years} />}
          </Section>
        )}
        {tab === "dupont" && (
          <Section state={dupont} isEmpty={(d) => !d.three_factor.length && !d.five_factor.length}
            empty="Not enough facts for a DuPont decomposition.">
            {(d) => (
              <>
                <h3>ROE</h3>
                <LineChart points={d.three_factor.map((r) => ({ x: r.fiscal_year, y: r.roe }))} format={(v) => percent(v, 0)} />
                <h3>3-factor</h3>
                <Transposed
                  years={d.three_factor.map((r) => r.fiscal_year)}
                  rows={[
                    ["ROE", d.three_factor.map((r) => percent(r.roe))],
                    ["Net margin", d.three_factor.map((r) => percent(r.net_margin))],
                    ["Asset turnover", d.three_factor.map((r) => ratio(r.asset_turnover))],
                    ["Equity multiplier", d.three_factor.map((r) => ratio(r.equity_multiplier))],
                  ]}
                />
                {d.five_factor.length > 0 && (
                  <>
                    <h3>5-factor</h3>
                    <Transposed
                      years={d.five_factor.map((r) => r.fiscal_year)}
                      rows={[
                        ["ROE", d.five_factor.map((r) => percent(r.roe))],
                        ["Tax burden", d.five_factor.map((r) => ratio(r.tax_burden, 3))],
                        ["Interest burden", d.five_factor.map((r) => ratio(r.interest_burden, 3))],
                        ["Operating margin", d.five_factor.map((r) => percent(r.operating_margin))],
                        ["Asset turnover", d.five_factor.map((r) => ratio(r.asset_turnover))],
                        ["Equity multiplier", d.five_factor.map((r) => ratio(r.equity_multiplier))],
                      ]}
                    />
                  </>
                )}
              </>
            )}
          </Section>
        )}
        {tab === "growth" && (
          <Section state={growth}>
            {(d) => {
              const metrics = [d.revenue_cagr_3y, d.revenue_cagr_5y, d.earnings_cagr_3y, d.earnings_cagr_5y,
                d.asset_cagr_3y, d.asset_cagr_5y].filter((m): m is GrowthMetric => m != null);
              if (!metrics.length && !d.roe_trend.length) return <div className="empty">Not enough history for growth metrics.</div>;
              return (
                <>
                  <div className="cards">
                    {metrics.map((m) => (
                      <div className="panel kpi" key={`${m.label}-${m.years}`}>
                        <div className="l">{m.label} CAGR · {m.years}y</div>
                        <div className={`v ${m.cagr < 0 ? "bad" : ""}`}>{percent(m.cagr)}</div>
                        <div className="s">FY{m.start_year} {money(m.start_value)} → FY{m.end_year} {money(m.end_value)}</div>
                      </div>
                    ))}
                  </div>
                  {d.roe_trend.length > 1 && (
                    <>
                      <h3>ROE trend</h3>
                      <LineChart points={d.roe_trend.map((r) => ({ x: r.fiscal_year, y: r.roe }))} format={(v) => percent(v, 0)} />
                    </>
                  )}
                </>
              );
            }}
          </Section>
        )}
        {tab === "prices" && (
          !wantPrices ? (
            <div className="notice">
              <p style={{ marginTop: 0 }}>Price ratios (P/E, P/B, EV/EBITDA, dividend yield) fetch closing prices from Yahoo Finance and can take a while.</p>
              <button className="primary" onClick={() => setWantPrices(true)}>Load price ratios</button>
            </div>
          ) : (
            <Section state={prices}>
              {(d) => (
                <>
                  <p className="muted" style={{ marginTop: 0 }}>
                    Ticker <code>{d.ticker}</code> · current price {d.current_price == null ? "–" : `RM ${d.current_price.toFixed(3)}`}
                    {" "}<button onClick={prices.reload} disabled={prices.loading}>{prices.loading ? "Loading…" : "Refresh"}</button>
                  </p>
                  {d.years.length ? (
                    <Transposed
                      years={d.years.map((y) => y.fiscal_year)}
                      rows={[
                        ["Period end", d.years.map((y) => y.period_end)],
                        ["Price", d.years.map((y) => ratio(y.price, 3))],
                        ["Shares", d.years.map((y) => money(y.shares))],
                        ["Market cap", d.years.map((y) => money(y.market_cap))],
                        ["EPS", d.years.map((y) => ratio(y.eps, 4))],
                        ["Book value", d.years.map((y) => money(y.book_value))],
                        ["EBITDA", d.years.map((y) => money(y.ebitda))],
                        ["Net debt", d.years.map((y) => money(y.net_debt))],
                        ["P/E", d.years.map((y) => ratio(y.pe_ratio))],
                        ["P/B", d.years.map((y) => ratio(y.pb_ratio))],
                        ["EV/EBITDA", d.years.map((y) => ratio(y.ev_ebitda))],
                        ["Dividend yield", d.years.map((y) => percent(y.dividend_yield, 2))],
                      ]}
                    />
                  ) : <div className="empty">No yearly price data.</div>}
                </>
              )}
            </Section>
          )
        )}
      </div>
    </>
  );
}

function Documents({ docs }: { docs: DocumentInfo[] }) {
  if (!docs.length) return <div className="empty">No documents ingested. Use <Link to="/upload">Upload</Link> to add an annual report.</div>;
  return (
    <div className="tablewrap">
      <table>
        <thead><tr><th>ID</th><th>Type</th><th>Source</th><th>File</th><th className="num">Pages</th><th>Status</th></tr></thead>
        <tbody>
          {docs.map((d) => (
            <tr key={d.id}>
              <td>{d.id}</td><td>{d.doc_type}</td><td>{d.source}</td>
              <td className="name" title={d.source_url ?? d.original_filename ?? ""}>
                {d.source_url
                  ? <a href={d.source_url} target="_blank" rel="noreferrer">{fileLabel(d.source_url, d.original_filename)}</a>
                  : d.original_filename ?? "–"}
              </td>
              <td className="num">{d.page_count ?? "–"}</td>
              <td><span className="pill">{d.status}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Scraped docs often have a temp-file original_filename; prefer the URL's basename. */
function fileLabel(url: string, original: string | null) {
  if (original && !/^tmp\w+\.pdf$/i.test(original)) return original;
  try { return decodeURIComponent(new URL(url).pathname.split("/").pop() || url); } catch { return url; }
}

// ------------------------------------------------------------------- facts
const STATEMENT_NAMES: Record<string, string> = {
  is: "Income statement", bs: "Balance sheet", cf: "Cash flow", other: "Other",
};

function parsePeriodKey(k: string): { fy: number; type: string } {
  const m = /^(\d{4}) \(([^)]+)\)$/.exec(k);
  return m ? { fy: Number(m[1]), type: m[2] } : { fy: NaN, type: k };
}

function FactsTable({ data }: { data: CompanyFacts }) {
  const parsed = useMemo(
    () => Object.entries(data.periods).map(([k, facts]) => ({ ...parsePeriodKey(k), facts })),
    [data],
  );
  const types = useMemo(() => {
    const quarter = [...new Set(parsed.map((p) => p.type).filter((t) => t !== "FY" && t !== "INSTANT"))].sort();
    return ["Annual", ...quarter];
  }, [parsed]);
  const bases = useMemo(() => [...new Set(parsed.flatMap((p) => p.facts.map((f) => f.basis)))].sort(), [parsed]);
  const [ptype, setPtype] = useState("Annual");
  const [basis, setBasis] = useState(bases.includes("CONSOLIDATED") ? "CONSOLIDATED" : bases[0] ?? "");
  const [q, setQ] = useState("");

  const { years, groups, cell } = useMemo(() => {
    const wanted = (t: string) => (ptype === "Annual" ? t === "FY" || t === "INSTANT" : t === ptype);
    const map = new Map<string, Map<number, { value: number | null; confidence: number | null }>>();
    const ys = new Set<number>();
    for (const p of parsed) {
      if (!wanted(p.type)) continue;
      for (const f of p.facts) {
        if (f.basis !== basis) continue;
        if (q && !f.concept_key.toLowerCase().includes(q.toLowerCase())) continue;
        ys.add(p.fy);
        let row = map.get(f.concept_key);
        if (!row) map.set(f.concept_key, (row = new Map()));
        if (!row.has(p.fy)) row.set(p.fy, { value: f.value, confidence: f.confidence });
      }
    }
    const g: Record<string, string[]> = {};
    for (const k of [...map.keys()].sort()) {
      const pre = k.split(".", 1)[0];
      (g[pre in STATEMENT_NAMES ? pre : "other"] ??= []).push(k);
    }
    return {
      years: [...ys].sort((a, b) => b - a),
      groups: (["is", "bs", "cf", "other"] as const).filter((s) => g[s]).map((s) => [s, g[s]] as const),
      cell: (k: string, y: number) => map.get(k)?.get(y),
    };
  }, [parsed, ptype, basis, q]);

  return (
    <>
      <div className="toolbar">
        <div className="seg">
          {types.map((t) => <button key={t} className={ptype === t ? "on" : ""} onClick={() => setPtype(t)}>{t}</button>)}
        </div>
        {bases.length > 1 && (
          <select value={basis} onChange={(e) => setBasis(e.target.value)}>
            {bases.map((b) => <option key={b} value={b}>{b.toLowerCase()}</option>)}
          </select>
        )}
        <input type="search" placeholder="Filter concept (e.g. revenue, bs.)" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      {!years.length ? <div className="empty">No facts for this selection.</div> : (
        <div className="tablewrap">
          <table>
            <thead>
              <tr><th>Concept</th>{years.map((y) => <th key={y} className="num">FY{y}</th>)}</tr>
            </thead>
            <tbody>
              {groups.map(([s, keys]) => (
                <FactGroup key={s} title={STATEMENT_NAMES[s]} keys={keys} years={years} cell={cell} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted" style={{ fontSize: 12 }}>
        Values in RM (unscaled). Annual view merges FY durations (IS/CF) with year-end instants (BS). Faded values have confidence &lt; 0.9.
      </p>
    </>
  );
}

function FactGroup({ title, keys, years, cell }: {
  title: string;
  keys: readonly string[];
  years: number[];
  cell: (k: string, y: number) => { value: number | null; confidence: number | null } | undefined;
}) {
  return (
    <>
      <tr className="group"><td colSpan={years.length + 1}>{title}</td></tr>
      {keys.map((k) => (
        <tr key={k}>
          <td><code>{k}</code></td>
          {years.map((y) => {
            const c = cell(k, y);
            const low = c?.confidence != null && c.confidence < 0.9;
            return (
              <td key={y} className={`num ${low ? "muted" : ""}`} title={c?.confidence != null ? `confidence ${c.confidence}` : undefined}>
                {c ? num(c.value) : ""}
              </td>
            );
          })}
        </tr>
      ))}
    </>
  );
}

// -------------------------------------------------------------- validation
function ValidationTable({ rows }: { rows: ValidationRow[] }) {
  const [failOnly, setFailOnly] = useState(false);
  const shown = failOnly ? rows.filter((r) => !r.passed) : rows;
  const passed = rows.filter((r) => r.passed).length;
  return (
    <>
      <div className="toolbar">
        <span><strong className={passed === rows.length ? "ok" : "warn"}>{passed}/{rows.length}</strong> rules passed</span>
        <label className="chk"><input type="checkbox" checked={failOnly} onChange={(e) => setFailOnly(e.target.checked)} /> Failures only</label>
      </div>
      <div className="tablewrap">
        <table>
          <thead><tr><th>FY</th><th>Period end</th><th>Rule</th><th>Result</th><th className="num">Expected</th>
            <th className="num">Actual</th><th className="num">Delta</th><th>Detail</th></tr></thead>
          <tbody>
            {shown.map((r, i) => (
              <tr key={`${r.period_end}-${r.rule_key}-${i}`}>
                <td>{r.fiscal_year}</td><td className="muted">{r.period_end}</td><td><code>{r.rule_key}</code></td>
                <td><span className={`pill ${r.passed ? "pass" : "fail"}`}>{r.passed ? "pass" : "fail"}</span></td>
                <td className="num">{num(r.expected)}</td><td className="num">{num(r.actual)}</td>
                <td className={`num ${r.delta ? "bad" : "muted"}`}>{num(r.delta)}</td>
                <td className="muted wrap-text">{r.detail ?? ""}</td>
              </tr>
            ))}
            {!shown.length && <tr><td colSpan={8} className="empty">No failures.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

// --------------------------------------------------------------- valuation
const VALUATION_ROWS: [keyof ValuationYear, string, "money" | "pct"][] = [
  ["ebit", "EBIT", "money"], ["ebitda", "EBITDA", "money"], ["effective_tax_rate", "Effective tax rate", "pct"],
  ["nopat", "NOPAT", "money"], ["dep_amort", "D&A", "money"], ["capex", "Capex", "money"],
  ["fcff", "FCFF", "money"], ["fcfe", "FCFE", "money"], ["net_debt", "Net debt", "money"],
];

function ValuationTable({ years }: { years: ValuationYear[] }) {
  const ys = [...years].sort((a, b) => a.fiscal_year - b.fiscal_year);
  return (
    <Transposed
      years={ys.map((y) => y.fiscal_year)}
      rows={VALUATION_ROWS.map(([k, l, kind]) => [l, ys.map((y) => (kind === "pct" ? percent(y[k]) : money(y[k])))])}
    />
  );
}

/** Metrics as rows, fiscal years as columns. */
function Transposed({ years, rows }: { years: number[]; rows: [string, string[]][] }) {
  return (
    <div className="tablewrap">
      <table>
        <thead><tr><th>Metric</th>{years.map((y) => <th key={y} className="num">FY{y}</th>)}</tr></thead>
        <tbody>
          {rows.map(([label, vals]) => (
            <tr key={label}><td>{label}</td>{vals.map((v, i) => <td key={i} className={`num ${v === "–" ? "muted" : ""}`}>{v}</td>)}</tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
