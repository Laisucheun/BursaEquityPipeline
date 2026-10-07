import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type { PeerComparison, PeerSnapshot } from "./api";
import { money, percent, ratio } from "./lib";

// Mirrors METRICS / PERCENT_METRICS / MONEY_METRICS in src/bursa/analysis/peers.py.
export const PEER_METRICS: [string, string][] = [
  ["revenue", "Revenue"], ["patami", "PATAMI"], ["roe", "ROE"], ["roa", "ROA"],
  ["net_margin", "Net margin"], ["operating_margin", "Op. margin"], ["asset_turnover", "Asset turn."],
  ["equity_multiplier", "Equity mult."], ["revenue_cagr_3y", "Rev. CAGR 3y"], ["earnings_cagr_3y", "Earn. CAGR 3y"],
  ["net_debt_ebitda", "ND/EBITDA"], ["fcf_margin", "FCF margin"],
];
const PERCENT = new Set(["roe", "roa", "net_margin", "operating_margin", "revenue_cagr_3y", "earnings_cagr_3y", "fcf_margin"]);
const MONEY = new Set(["revenue", "patami"]);
/** Metrics where a lower value is the healthier one (percentile colouring flips). */
const LOWER_BETTER = new Set(["equity_multiplier", "net_debt_ebitda"]);
/** Size metrics: shade by percentile without a good/bad judgement. */
const NEUTRAL = new Set(["revenue", "patami"]);

export function fmtMetric(m: string, v: number | null | undefined): string {
  if (v == null) return "–";
  if (MONEY.has(m)) return money(v);
  if (PERCENT.has(m)) return percent(v);
  return `${ratio(v)}x`;
}

/** Percentile band class: top quartile p4 … bottom quartile p1 (direction-adjusted). */
function band(m: string, p: number | null | undefined): string {
  if (p == null) return "";
  const q = LOWER_BETTER.has(m) ? 100 - p : p;
  const b = q >= 75 ? 4 : q >= 50 ? 3 : q > 25 ? 2 : 1;
  return NEUTRAL.has(m) ? `pn${b}` : `pq${b}`;
}

type SortKey = "name" | "fy" | string;

export function PeerTable({ data, highlight }: { data: PeerComparison; highlight?: string }) {
  const [sortKey, setSortKey] = useState<SortKey>("revenue");
  const [dir, setDir] = useState(-1);
  const metrics = useMemo(
    () => PEER_METRICS.filter(([m]) => data.rows.some((r) => r.metrics[m] != null || r.not_applicable.includes(m))),
    [data],
  );
  const rows = useMemo(() => {
    const val = (r: PeerSnapshot): number | string =>
      sortKey === "name" ? r.name : sortKey === "fy" ? r.fiscal_year ?? 0 : r.metrics[sortKey] ?? Number.POSITIVE_INFINITY * dir; // nulls last either way
    return [...data.rows].sort((a, b) => {
      const x = val(a), y = val(b);
      return (x > y ? 1 : x < y ? -1 : 0) * dir;
    });
  }, [data, sortKey, dir]);
  const onSort = (k: SortKey) => {
    setDir(sortKey === k ? -dir : k === "name" ? 1 : -1);
    setSortKey(k);
  };
  const arrow = (k: SortKey) => (sortKey === k ? (dir > 0 ? " ▲" : " ▼") : "");
  const fyMix = Object.entries(data.fiscal_years).sort(([a], [b]) => Number(b) - Number(a));

  return (
    <>
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <span className="muted">
          {data.rows.length} compan{data.rows.length === 1 ? "y" : "ies"}
          {fyMix.length ? ` · ${fyMix.map(([y, n]) => `FY${y}: ${n}`).join(", ")}` : ""}
          {data.requested_fy ? ` · requested FY${data.requested_fy}` : ""}
        </span>
        <div className="legend">
          <span>Percentile in set:</span>
          <span><i className="cov pq4" />top quartile</span><span><i className="cov pq1" />bottom</span>
          <span><i className="cov pn4" />size (no judgement)</span>
          <span><span className="bad">⚑</span> implausible, excluded</span>
        </div>
      </div>
      <div className="tablewrap">
        <table className="peers">
          <thead>
            <tr>
              <th className="sortable" onClick={() => onSort("name")}>Company{arrow("name")}</th>
              <th className="sortable num" onClick={() => onSort("fy")}>FY{arrow("fy")}</th>
              {metrics.map(([m, l]) => (
                <th key={m} className="sortable num" onClick={() => onSort(m)}
                  title={LOWER_BETTER.has(m) ? "lower is better" : undefined}>{l}{arrow(m)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.stock_code} className={r.stock_code === highlight ? "hl" : ""}>
                <td className="name" title={[r.name, r.industry, r.profile !== "general" ? r.profile : null, ...r.warnings].filter(Boolean).join("\n")}>
                  <Link to={`/company/${r.stock_code}?tab=peers`}>{r.stock_code}</Link> {r.name}
                  {r.profile !== "general" && <span className="pill MISSING" style={{ marginLeft: 6 }}>{r.profile}</span>}
                  {r.warnings.length > 0 && <span className="warn" title={r.warnings.join("\n")}> ⚠</span>}
                </td>
                <td className="num muted" title={r.period_end ?? undefined}>{r.fiscal_year ?? "–"}</td>
                {metrics.map(([m]) => {
                  if (r.not_applicable.includes(m)) return <td key={m} className="num muted" title="not applicable for this profile">n/a</td>;
                  const flag = r.flags[m];
                  const p = r.percentiles[m];
                  return (
                    <td key={m} className={`num ${flag ? "" : band(m, p)}`}
                      title={flag ? `Flagged: ${flag}` : p != null ? `percentile ${p.toFixed(0)}` : undefined}>
                      {flag && <span className="bad">⚑ </span>}
                      <span className={flag ? "muted" : ""}>{fmtMetric(m, r.metrics[m])}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
          <tfoot>
            {(["q3", "median", "q1"] as const).map((s) => (
              <tr key={s} className="stat">
                <td>{s === "median" ? "Median" : s === "q1" ? "Lower quartile" : "Upper quartile"}</td>
                <td />
                {metrics.map(([m]) => {
                  const st = data.stats[m];
                  return (
                    <td key={m} className="num" title={st ? `n=${st.n}${st.n_flagged ? `, ${st.n_flagged} flagged` : ""} · min ${fmtMetric(m, st.min)} · max ${fmtMetric(m, st.max)}` : undefined}>
                      {fmtMetric(m, st?.[s])}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tfoot>
        </table>
      </div>
      {(data.notes.length > 0 || data.missing.length > 0) && (
        <ul className="notes">
          {data.notes.map((n) => <li key={n}>{n}</li>)}
          {data.missing.length > 0 && <li>No facts for: {data.missing.join(", ")}</li>}
        </ul>
      )}
    </>
  );
}
