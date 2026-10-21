import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, type HeadlineRow, type ResultsReport as Report } from "../api";
import { LineChart, Section } from "../components";
import { money, useAsync } from "../lib";
import { reportLink, rm, signed } from "./Feed";

const TONE_LABEL = { up: "Improved", down: "Weaker", flat: "Flat", new: "First year" } as const;
const LEVEL_LABEL = { good: "positive", watch: "watch", data: "data" } as const;

export default function ResultsReport() {
  const { code = "" } = useParams();
  const [params] = useSearchParams();
  const fy = params.get("fy") ? Number(params.get("fy")) : undefined;
  const report = useAsync(() => api.resultsReport(code, fy), [code, fy]);

  return (
    <article className="report">
      <p style={{ marginTop: 0 }}><Link to="/feed">← Results feed</Link></p>
      <Section state={report}>{(r) => <Body r={r} />}</Section>
    </article>
  );
}

function Body({ r }: { r: Report }) {
  const end = new Date(`${r.period_end}T00:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
  const revenue = r.history.filter((h) => h.revenue != null).map((h) => ({ x: h.fiscal_year, y: h.revenue! }));
  const patami = r.history.filter((h) => h.patami != null).map((h) => ({ x: h.fiscal_year, y: h.patami! }));
  const sc = r.sector_context;

  return (
    <>
      <h1>{r.name} ({r.stock_code}) FY{r.fiscal_year} results</h1>
      <p className="muted" style={{ marginTop: 4 }}>
        Year ended {end}{r.sector ? ` · ${r.sector}` : ""} · <span className={`pill tone-${r.tone}`}>{TONE_LABEL[r.tone]}</span>
      </p>
      {r.available_years.length > 1 && (
        <div className="seg" style={{ marginBottom: 12 }}>
          {r.available_years.map((y) => (
            <Link key={y} to={reportLink(r.stock_code, y)} className={`segl ${y === r.fiscal_year ? "on" : ""}`}>FY{y}</Link>
          ))}
        </div>
      )}

      <p className="bottom-line"><strong>Bottom line:</strong> {r.bottom_line}</p>

      <h2 className="rh">Headline numbers</h2>
      <div className="tablewrap">
        <table>
          <thead>
            <tr><th>Measure</th><th className="num">FY{r.fiscal_year}</th>
              <th className="num">{r.prior_fiscal_year ? `FY${r.prior_fiscal_year}` : "Prior"}</th><th className="num">Change</th></tr>
          </thead>
          <tbody>{r.rows.map((row) => <Row key={row.measure} row={row} />)}</tbody>
        </table>
      </div>
      <p className="muted small">
        Consolidated figures from the annual report. PATAMI = profit attributable to owners. DPS is dividends paid in the year
        (cash-flow basis), not declared.
      </p>

      {r.observations.length > 0 && (
        <>
          <h2 className="rh">What to dig into</h2>
          <ul className="obs">
            {r.observations.map((o) => (
              <li key={o.title} className={o.level}>
                <span className={`pill lvl-${o.level}`}>{LEVEL_LABEL[o.level]}</span> <strong>{o.title}.</strong> {o.text}
              </li>
            ))}
          </ul>
        </>
      )}

      {sc && (
        <>
          <h2 className="rh">Sector read-through</h2>
          <dl className="dl">
            <dt>Sector</dt><dd>{sc.sector} ({sc.companies} companies on file)</dd>
            {sc.median_revenue_growth != null && (
              <><dt>Median revenue growth, FY{r.fiscal_year}</dt>
                <dd>{signed(sc.median_revenue_growth)}{sc.revenue_growth_rank ? ` · this company ranks ${sc.revenue_growth_rank} of ${sc.revenue_growth_n}` : ""}</dd></>
            )}
            {sc.median_net_margin != null && (
              <><dt>Median net margin, FY{r.fiscal_year}</dt>
                <dd>{(sc.median_net_margin * 100).toFixed(1)}%{sc.net_margin_rank ? ` · this company ranks ${sc.net_margin_rank} of ${sc.net_margin_n}` : ""}</dd></>
            )}
          </dl>
          <p className="small"><Link to={`/company/${encodeURIComponent(r.stock_code)}?tab=peers`}>Full peer comparison →</Link></p>
        </>
      )}

      {(revenue.length > 1 || patami.length > 1) && (
        <>
          <h2 className="rh">Trend</h2>
          <div className="grid two" style={{ marginTop: 0 }}>
            {revenue.length > 1 && <div><h3>Revenue</h3><LineChart points={revenue} format={(v) => money(v)} /></div>}
            {patami.length > 1 && <div><h3>PATAMI</h3><LineChart points={patami} format={(v) => money(v)} /></div>}
          </div>
        </>
      )}

      <h2 className="rh">Data quality</h2>
      {r.validation.run ? (
        <p>
          <strong className={r.validation.failures.length ? "warn" : "ok"}>{r.validation.passed}/{r.validation.run}</strong>{" "}
          accounting-identity checks passed for FY{r.fiscal_year}.{" "}
          <Link to={`/company/${encodeURIComponent(r.stock_code)}?tab=validation`}>Validation detail →</Link>
        </p>
      ) : <p className="muted">No validation results for this year (run <code>bursa validate facts</code>).</p>}
      {r.validation.failures.length > 0 && (
        <ul className="notes">{r.validation.failures.map((f, i) => <li key={i}><code>{f.rule}</code> {f.detail}</li>)}</ul>
      )}
      {r.notes.length > 0 && <ul className="notes">{r.notes.map((n) => <li key={n}>{n}</li>)}</ul>}

      <p className="muted feed-foot">
        Generated deterministically from extracted annual report facts - every figure traces to a fact row on the{" "}
        <Link to={`/company/${encodeURIComponent(r.stock_code)}?tab=facts`}>Facts tab</Link>. Extraction errors are possible; not investment advice.
      </p>
    </>
  );
}

function Row({ row }: { row: HeadlineRow }) {
  const val = (v: number | null) =>
    v == null ? "–" : row.kind === "pct" ? `${(v * 100).toFixed(1)}%` : row.kind === "sen" ? v.toFixed(2) : rm(v);
  let ch = "–", cls = "muted";
  if (row.change_kind === "pp" && row.change != null) {
    ch = `${row.change >= 0 ? "+" : ""}${row.change.toFixed(1)} pp`;
    cls = "";
  } else if (row.change_kind === "growth" && row.change != null) {
    ch = signed(row.change);
    cls = "";
  } else if (row.change_kind !== "none") {
    const words: Record<string, string> = {
      turnaround: "back to profit", to_loss: "to loss", loss_narrowed: "loss narrowed", loss_widened: "loss widened",
    };
    ch = words[row.change_kind] ?? "–";
    cls = row.change_kind === "turnaround" || row.change_kind === "loss_narrowed" ? "ok" : "bad";
  }
  return (
    <tr>
      <td>{row.measure}</td>
      <td className="num">{val(row.current)}</td>
      <td className="num muted">{val(row.prior)}</td>
      <td className={`num ${cls}`}>{ch}</td>
    </tr>
  );
}
