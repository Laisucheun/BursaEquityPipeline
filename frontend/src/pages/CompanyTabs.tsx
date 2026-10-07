// Dividends, 5-year check, and Peers tabs of the company detail page.
import { useMemo, useState } from "react";
import { api, type DividendHistory, type FiveYearCheck, type FiveYearResult, type YearDividend } from "../api";
import { ErrorBox, Section } from "../components";
import { money, num, percent, ratio, useAsync } from "../lib";
import { PeerTable } from "../peers";

// --------------------------------------------------------------- dividends
export function DividendsTab({ code }: { code: string }) {
  const base = useAsync(() => api.dividends(code), [code]);
  // Yield needs closing prices (network, several seconds): only on request.
  const [wantYield, setWantYield] = useState(false);
  const withYield = useAsync(() => api.dividends(code, true), [code], wantYield);
  const state = withYield.data ? withYield : base;

  return (
    <Section state={state} isEmpty={(d) => !d.years.length} empty="No annual facts for this company.">
      {(d) => (
        <>
          <DividendSummary d={d} />
          <div className="toolbar" style={{ marginTop: 12 }}>
            {!withYield.data && (
              <button onClick={() => (wantYield ? withYield.reload() : setWantYield(true))} disabled={withYield.loading}>
                {withYield.loading ? <><span className="spin" /> Fetching prices…</> : "Load yield (fetches prices)"}
              </button>
            )}
            {withYield.data && <span className="muted">Yield uses the close on or before each period end.</span>}
            <ErrorBox error={withYield.error} />
          </div>
          <DpsChart years={d.years} />
          <DividendTable years={d.years} withPrices={!!withYield.data} />
          {d.notes.length > 0 && <ul className="notes">{d.notes.map((n) => <li key={n}>{n}</li>)}</ul>}
        </>
      )}
    </Section>
  );
}

function DividendSummary({ d }: { d: DividendHistory }) {
  return (
    <div className="cards">
      <div className="panel kpi">
        <div className="l">Current streak</div>
        <div className="v">{d.streak} yr{d.streak === 1 ? "" : "s"}</div>
        <div className="s">{d.streak_end ? `ending FY${d.streak_end}` : "no dividends found"}</div>
      </div>
      <div className="panel kpi">
        <div className="l">Longest streak</div>
        <div className="v">{d.longest_streak} yr{d.longest_streak === 1 ? "" : "s"}</div>
        <div className="s">consecutive fiscal years with a dividend</div>
      </div>
      <div className="panel kpi">
        <div className="l">Median payout</div>
        <div className="v">{percent(d.median_payout, 0)}</div>
        <div className="s">current streak, unflagged years{d.is_reit ? " · REIT (DPU)" : ""}</div>
      </div>
    </div>
  );
}

/** Inline-SVG bar chart of DPS (sen) per fiscal year. Flagged years are drawn in the warning colour. */
function DpsChart({ years }: { years: YearDividend[] }) {
  const pts = years.filter((y) => y.dps_sen != null);
  if (pts.length < 2) return null;
  const first = pts[0].fiscal_year, last = pts[pts.length - 1].fiscal_year;
  const span = years.filter((y) => y.fiscal_year >= first && y.fiscal_year <= last);
  const W = 640, H = 170, padL = 40, padR = 8, padT = 14, padB = 22;
  const max = Math.max(...pts.map((p) => p.dps_sen!)) || 1;
  const slot = (W - padL - padR) / span.length;
  const bw = Math.max(4, Math.min(28, slot - 6));
  const sy = (v: number) => padT + (1 - v / max) * (H - padT - padB);
  const base = H - padB;
  const labelEvery = Math.ceil(span.length / 12);
  return (
    <>
      <h3>Dividend per share (sen)</h3>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Dividend per share by fiscal year">
        {[0, max / 2, max].map((t) => (
          <g key={t}>
            <line className="axis" x1={padL} x2={W - padR} y1={sy(t)} y2={sy(t)} />
            <text x={padL - 6} y={sy(t) + 4} textAnchor="end">{t.toFixed(t < 10 ? 1 : 0)}</text>
          </g>
        ))}
        {span.map((y, i) => {
          const cx = padL + slot * (i + 0.5);
          const v = y.dps_sen;
          const top = v == null ? base : sy(v);
          const h = Math.max(0, base - top);
          const r = Math.min(4, h / 2, bw / 2);
          const flagged = y.flags.length > 0;
          return (
            <g key={y.fiscal_year}>
              <rect className="hit" x={cx - slot / 2} y={padT} width={slot} height={base - padT}>
                <title>{`FY${y.fiscal_year}: ${v == null ? (y.paid === false ? "no dividend" : "no data") : `${v.toFixed(2)} sen`}${flagged ? `\n⚑ ${y.flags.join("\n⚑ ")}` : ""}`}</title>
              </rect>
              {v != null && h > 0 && (
                <path className={`barmark${flagged ? " flagged" : ""}`} pointerEvents="none"
                  d={`M${cx - bw / 2},${base}V${top + r}Q${cx - bw / 2},${top} ${cx - bw / 2 + r},${top}H${cx + bw / 2 - r}Q${cx + bw / 2},${top} ${cx + bw / 2},${top + r}V${base}Z`} />
              )}
              {v == null && <text x={cx} y={base - 4} textAnchor="middle" className="nodata">·</text>}
              {(i % labelEvery === 0 || i === span.length - 1) && <text x={cx} y={H - 6} textAnchor="middle">{y.fiscal_year}</text>}
            </g>
          );
        })}
      </svg>
      <div className="legend" style={{ marginBottom: 8 }}>
        <span><i className="cov swatch-accent" />DPS</span>
        <span><i className="cov swatch-warn" />flagged year (hover for why)</span>
      </div>
    </>
  );
}

function DividendTable({ years, withPrices }: { years: YearDividend[]; withPrices: boolean }) {
  const [hideEmpty, setHideEmpty] = useState(true);
  const rows = [...years].reverse().filter((y) => !hideEmpty || y.paid != null || y.eps_sen != null);
  const src = (y: YearDividend, k: string) => y.sources[k];
  const cell = (y: YearDividend, k: string, text: string) => (
    <td className={`num ${text === "–" ? "muted" : ""}`} title={src(y, k)}>{text}</td>
  );
  return (
    <>
      <div className="toolbar">
        <label className="chk"><input type="checkbox" checked={hideEmpty} onChange={(e) => setHideEmpty(e.target.checked)} /> Hide years with no dividend or EPS data</label>
        <span className="muted" style={{ fontSize: 12 }}>Hover a figure to see its source.</span>
      </div>
      <div className="tablewrap">
        <table>
          <thead>
            <tr>
              <th>FY</th><th>Paid</th><th className="num">Dividends</th><th>Basis</th><th className="num">DPS (sen)</th>
              <th className="num">EPS (sen)</th><th className="num">Payout</th><th className="num">Cover</th><th className="num">DPS growth</th>
              {withPrices && <><th className="num">Price</th><th className="num">Yield</th></>}
              <th>Flags</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((y) => (
              <tr key={y.fiscal_year}>
                <td title={y.period_end}>{y.fiscal_year}</td>
                <td>{y.paid == null ? <span className="muted">?</span> : y.paid ? <span className="ok">yes</span> : <span className="muted">no</span>}</td>
                {cell(y, "dividends", money(y.dividends))}
                <td className="muted">{y.dividends_basis ?? ""}</td>
                {cell(y, "dps_sen", ratio(y.dps_sen))}
                {cell(y, "eps_sen", ratio(y.eps_sen))}
                {cell(y, "payout_ratio", percent(y.payout_ratio, 0))}
                {cell(y, "dividend_cover", y.dividend_cover == null ? "–" : `${ratio(y.dividend_cover, 1)}x`)}
                {cell(y, "dps_growth", percent(y.dps_growth, 0))}
                {withPrices && <>
                  {cell(y, "price", ratio(y.price, 3))}
                  {cell(y, "dividend_yield", percent(y.dividend_yield, 2))}
                </>}
                <td className="wrap-text">{y.flags.map((f) => <div key={f} className="warn" style={{ fontSize: 12 }}>⚑ {f}</div>)}</td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={withPrices ? 12 : 10} className="empty">No dividend data.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ---------------------------------------------------------- 5-year check
const FY_CLASSES = ["MATCH", "CLOSE", "MISMATCH", "SCALE_ERROR", "ONLY_IN_SUMMARY"] as const;
const FY_SHORT: Record<string, string> = { MATCH: "✓", CLOSE: "≈", MISMATCH: "✗", SCALE_ERROR: "×10ⁿ", ONLY_IN_SUMMARY: "–" };

export function FiveYearTab({ code }: { code: string }) {
  const [years, setYears] = useState(1);
  const [run, setRun] = useState(false);
  const res = useAsync(() => api.fiveYear(code, years), [code, years], run);

  return (
    <>
      <div className="toolbar">
        <label className="chk">Reports
          <select value={years} onChange={(e) => setYears(Number(e.target.value))}>
            <option value={1}>newest only</option><option value={2}>newest 2</option>
            <option value={3}>newest 3</option><option value={0}>all</option>
          </select>
        </label>
        {!run ? (
          <button className="primary" onClick={() => setRun(true)}>Run 5-year check</button>
        ) : (
          <button onClick={res.reload} disabled={res.loading}>{res.loading ? <><span className="spin" /> Parsing PDFs…</> : "Re-run"}</button>
        )}
        <span className="muted" style={{ fontSize: 12 }}>
          Extracts the five-year financial summary page(s) from the annual report PDFs and compares each figure with our facts. Read-only; takes a few seconds.
        </span>
      </div>
      {run && (
        res.loading && !res.data
          ? <div className="muted"><span className="spin" /> Parsing annual report PDFs…</div>
          : <Section state={res}>{(d) => <FiveYearResultView d={d} />}</Section>
      )}
    </>
  );
}

function FiveYearResultView({ d }: { d: FiveYearResult }) {
  const { concepts, yrs, cell } = useMemo(() => {
    const m = new Map<string, Map<number, FiveYearCheck>>();
    const ys = new Set<number>();
    for (const c of d.checks) {
      ys.add(c.fiscal_year);
      let row = m.get(c.concept_key);
      if (!row) m.set(c.concept_key, (row = new Map()));
      row.set(c.fiscal_year, c);
    }
    return {
      concepts: [...m.keys()].sort(),
      yrs: [...ys].sort((a, b) => a - b),
      cell: (k: string, y: number) => m.get(k)?.get(y),
    };
  }, [d]);
  const comparable = FY_CLASSES.slice(0, 4).reduce((n, k) => n + (d.counts[k] ?? 0), 0);

  return (
    <>
      <div className="cards">
        <div className="panel kpi">
          <div className="l">Agreement</div>
          <div className={`v ${d.agreement == null ? "" : d.agreement >= 0.95 ? "ok" : d.agreement >= 0.8 ? "warn" : "bad"}`}>{percent(d.agreement, 1)}</div>
          <div className="s">{d.counts.MATCH ?? 0} match of {comparable} comparable</div>
        </div>
        <div className="panel kpi">
          <div className="l">Summary pages found</div>
          <div className="v">{d.documents_with_summary}/{d.documents_scanned}</div>
          <div className="s">{d.summary_pages.length ? `page${d.summary_pages.length > 1 ? "s" : ""} ${d.summary_pages.join(", ")}` : "no summary page detected"}</div>
        </div>
        <div className="panel kpi">
          <div className="l">Year alignment</div>
          <div className={`v ${d.year_shift ? "bad" : "ok"}`}>{d.year_shift ? `shifted ${d.year_shift > 0 ? "+" : ""}${d.year_shift} FY` : "aligned"}</div>
          <div className="s">{d.year_shift ? "summary years line up with our facts one fiscal year off – check FY labelling" : "summary years match our fiscal years"}</div>
        </div>
      </div>
      <div className="legend" style={{ margin: "12px 0" }}>
        {FY_CLASSES.map((k) => (
          <span key={k}><span className={`fy5 ${k}`}>{FY_SHORT[k]}</span>{k.replace(/_/g, " ").toLowerCase()} ({d.counts[k] ?? 0})</span>
        ))}
        <span><span className="fy5">R</span>restated</span>
      </div>
      {d.documents.some((x) => x.error) && (
        <ul className="notes">
          {d.documents.filter((x) => x.error).map((x) => <li key={x.document_id} className="bad">Document {x.document_id} (FY{x.report_year}): {x.error}</li>)}
        </ul>
      )}
      {!concepts.length ? (
        <div className="empty">No five-year summary figures could be compared for this company.</div>
      ) : (
        <div className="tablewrap">
          <table className="fy5grid">
            <thead><tr><th>Concept</th>{yrs.map((y) => <th key={y} className="num">FY{y}</th>)}</tr></thead>
            <tbody>
              {concepts.map((k) => (
                <tr key={k}>
                  <td><code>{k}</code></td>
                  {yrs.map((y) => {
                    const c = cell(k, y);
                    if (!c) return <td key={y} />;
                    const dev = c.deviation_pct == null ? "" : ` (${(c.deviation_pct * 100).toFixed(2)}%)`;
                    return (
                      <td key={y} className="num">
                        <span className={`fy5 ${c.classification}`}
                          title={`${c.classification}${dev}\nsummary: ${num(c.summary_value)}\nours: ${num(c.our_value)}\n${c.detail}\np.${c.page_no}${c.document_id ? ` of document ${c.document_id}` : ""}${c.restated ? "\nrestated in a later report" : ""}`}>
                          {FY_SHORT[c.classification] ?? "?"}{c.restated ? " R" : ""}
                        </span>
                        <div className="meta">{money(c.summary_value)}</div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="muted" style={{ fontSize: 12 }}>Cells show the summary-page value; hover for ours, the deviation and the source page.</p>
    </>
  );
}

// ------------------------------------------------------------------ peers
export function PeersTab({ code }: { code: string }) {
  const [peerInput, setPeerInput] = useState("");
  const [fyInput, setFyInput] = useState("");
  const [query, setQuery] = useState<{ peers: string[]; fy?: number }>({ peers: [] });
  const res = useAsync(() => api.companyPeers(code, query.peers, query.fy), [code, query.peers.join(","), query.fy]);
  const apply = () => setQuery({
    peers: peerInput.split(/[\s,;]+/).filter(Boolean),
    fy: /^\d{4}$/.test(fyInput.trim()) ? Number(fyInput.trim()) : undefined,
  });

  return (
    <>
      <form className="toolbar" onSubmit={(e) => { e.preventDefault(); apply(); }}>
        <input type="text" placeholder="Peer codes, e.g. 5209, 6033 (blank = sector peers)" value={peerInput}
          onChange={(e) => setPeerInput(e.target.value)} style={{ minWidth: 300 }} />
        <input type="text" placeholder="FY (latest)" value={fyInput} onChange={(e) => setFyInput(e.target.value)} style={{ width: 100 }} />
        <button type="submit" className="primary" disabled={res.loading}>{res.loading ? "Loading…" : "Compare"}</button>
      </form>
      <Section state={res} isEmpty={(d) => !d.rows.length} empty="No peers with facts.">
        {(d) => <><h3 style={{ marginTop: 0 }}>{d.title}</h3><PeerTable data={d} highlight={code} /></>}
      </Section>
    </>
  );
}
