import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type CoverageRow, type ManualDownload, type Progress } from "../api";
import { Bar, CoverageCells, InlineCode } from "../components";
import { ago, dur, fmt, pct } from "../lib";

type SortKey = "stock_code" | "name" | "documents" | "facts" | "years" | "valid" | "bench" | "last_activity";

const COLS: [SortKey, string, string?][] = [
  ["stock_code", "Code"], ["name", "Name"], ["documents", "Docs", "num"], ["facts", "Facts", "num"],
  ["years", "Coverage"], ["valid", "Validation", "num"], ["bench", "Benchmark", "num"], ["last_activity", "Updated"],
];

function sortVal(r: CoverageRow, k: SortKey): number | string {
  switch (k) {
    case "valid": return r.validation.run ? r.validation.passed / r.validation.run : -1;
    case "bench": return (r.benchmark.MATCH ?? 0) + (r.benchmark.CLOSE ?? 0);
    case "years": return r.years.length;
    case "last_activity": return r.last_activity ?? "";
    default: return r[k];
  }
}

function yearRange(rows: CoverageRow[]): number[] {
  const ys = rows.flatMap((r) => r.years);
  if (!ys.length) return [];
  const max = Math.max(...ys);
  const min = Math.max(Math.min(...ys), max - 9);
  return Array.from({ length: max - min + 1 }, (_, i) => min + i);
}

/** Poll /api/progress: every 2s while a job is RUNNING, else every 10s. */
function useProgress() {
  const [data, setData] = useState<Progress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updated, setUpdated] = useState<Date | null>(null);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      window.clearTimeout(timer.current);
      let running = false;
      try {
        const d = await api.progress();
        if (!alive) return;
        running = d.jobs.some((j) => j.status === "RUNNING");
        setData(d);
        setError(null);
        setUpdated(new Date());
      } catch (e) {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
      }
      timer.current = window.setTimeout(tick, running ? 2000 : 10000);
    };
    const onVis = () => { if (!document.hidden) tick(); };
    document.addEventListener("visibilitychange", onVis);
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  return { data, error, updated };
}

export default function Dashboard() {
  const { data, error, updated } = useProgress();
  const running = !!data?.jobs.some((j) => j.status === "RUNNING");

  return (
    <>
      <div className="pagehead">
        <h1>Pipeline progress</h1>
        <div className="status">
          <span className={`dot${error ? " err" : running || data?.activity.active ? " live" : ""}`} />
          <span>
            {error
              ? `can't reach API (${error}) – retrying`
              : updated
                ? `${running ? "job running · " : ""}updated ${updated.toLocaleTimeString()}`
                : "loading…"}
          </span>
        </div>
      </div>
      {data && (
        <>
          <Kpis d={data} />
          <section className="grid two">
            <div className="panel"><h2>Jobs</h2><Jobs d={data} /></div>
            <div className="panel"><h2>Pipeline funnel</h2><Funnel d={data} /></div>
          </section>
          <section className="grid"><CoverageTable rows={data.companies} /></section>
          <section className="grid"><ManualDownloads rows={data.manual_downloads} /></section>
          <section className="grid two">
            <div className="panel"><h2>Recent extraction runs</h2><Runs d={data} /></div>
            <div className="panel"><h2>Roadmap</h2><Roadmap d={data} /></div>
          </section>
        </>
      )}
    </>
  );
}

function Kpis({ d }: { d: Progress }) {
  const withFacts = d.funnel.find((f) => f.stage === "Has facts")?.count ?? 0;
  const b = d.benchmark;
  const good = (b.MATCH ?? 0) + (b.CLOSE ?? 0);
  const compared = good + (b.MISMATCH ?? 0) + (b.SCALE_ERROR ?? 0);
  const tiles: [string, string, string, number | null][] = [
    ["Companies with facts", fmt(withFacts), `of ${fmt(d.totals.companies)} on watchlist`, pct(withFacts, d.totals.companies)],
    ["Documents", fmt(d.totals.documents),
      Object.entries(d.document_status).map(([k, v]) => `${k.toLowerCase()} ${fmt(v)}`).join(" · "), null],
    ["Facts", fmt(d.totals.facts), `${fmt(d.totals.periods)} periods`, null],
    ["Validation pass rate", `${pct(d.validation.passed, d.validation.run).toFixed(1)}%`,
      `${fmt(d.validation.passed)} / ${fmt(d.validation.run)} rules`, pct(d.validation.passed, d.validation.run)],
    ["Benchmark agreement", compared ? `${pct(good, compared).toFixed(1)}%` : "–",
      `${fmt(good)} match/close · ${fmt(b.MISMATCH ?? 0)} mismatch · ${fmt(b.MISSING ?? 0)} missing`,
      compared ? pct(good, compared) : null],
  ];
  return (
    <section className="grid kpis">
      {tiles.map(([l, v, s, p]) => (
        <div className="panel kpi" key={l}>
          <div className="l">{l}</div>
          <div className="v">{v}</div>
          {p != null && <Bar value={p} style={{ margin: "6px 0 2px" }} />}
          <div className="s">{s}</div>
        </div>
      ))}
    </section>
  );
}

function Jobs({ d }: { d: Progress }) {
  if (!d.jobs.length)
    return <div className="empty">No tracked jobs yet. Run <code>bursa normalize facts</code> to see live progress here.</div>;
  return (
    <>
      {d.jobs.map((j) => {
        const p = pct(j.done, j.total);
        return (
          <div className="job" key={j.id}>
            <div className="row1">
              <div><strong>{j.kind}</strong> <span className={`pill ${j.status}`}>{j.status.toLowerCase()}</span></div>
              <div className="meta">{fmt(j.done)} / {fmt(j.total)} · {p.toFixed(0)}%</div>
            </div>
            <Bar value={p} big />
            <div className="meta" style={{ marginTop: 6 }}>
              {j.status === "RUNNING"
                ? `now: ${j.current || "starting…"} · elapsed ${dur(j.elapsed_s)} · ETA ${dur(j.eta_s)}`
                : `started ${ago(j.started_at)} · took ${dur(j.elapsed_s)}`}
            </div>
            {j.error && <div className="err-box">{j.error}</div>}
          </div>
        );
      })}
    </>
  );
}

function Funnel({ d }: { d: Progress }) {
  const top = d.funnel[0]?.count || 1;
  return (
    <div className="funnel">
      {d.funnel.map((f) => (
        <div className="f" key={f.stage}>
          <div>{f.stage}</div>
          <Bar value={pct(f.count, top)} />
          <div className="n">{fmt(f.count)}</div>
        </div>
      ))}
      <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>Bars are relative to the full watchlist.</div>
    </div>
  );
}

function CoverageTable({ rows }: { rows: CoverageRow[] }) {
  const [q, setQ] = useState("");
  const [onlyFacts, setOnlyFacts] = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>("facts");
  const [sortDir, setSortDir] = useState(-1);
  const years = useMemo(() => yearRange(rows), [rows]);

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const out = rows.filter((r) => (!onlyFacts || r.facts) &&
      (!needle || r.stock_code.toLowerCase().includes(needle) || r.name.toLowerCase().includes(needle)));
    out.sort((a, b) => {
      const x = sortVal(a, sortKey), y = sortVal(b, sortKey);
      return (x > y ? 1 : x < y ? -1 : 0) * sortDir;
    });
    return out;
  }, [rows, q, onlyFacts, sortKey, sortDir]);

  const onSort = (k: SortKey) => {
    setSortDir(sortKey === k ? -sortDir : k === "stock_code" || k === "name" ? 1 : -1);
    setSortKey(k);
  };

  return (
    <div className="panel">
      <h2>Company coverage</h2>
      <div className="toolbar">
        <input type="search" placeholder="Filter by code or name" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="chk">
          <input type="checkbox" checked={onlyFacts} onChange={(e) => setOnlyFacts(e.target.checked)} /> Only companies with facts
        </label>
        <div className="legend">
          <span>Statements per year:</span>
          <span><i className="cov c0" />0</span><span><i className="cov c1" />1</span>
          <span><i className="cov c2" />2</span><span><i className="cov c3" />IS+BS+CF</span>
        </div>
      </div>
      <div className="tablewrap">
        <table>
          <thead>
            <tr>
              {COLS.map(([k, l, cls]) => (
                <th key={k} className={`sortable ${cls ?? ""}`} onClick={() => onSort(k)}>
                  {l}
                  {k === "years" && years.length > 0 && <span className="muted"> {years[0]}–{years.at(-1)}</span>}
                  {sortKey === k ? (sortDir > 0 ? " ▲" : " ▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((r) => {
              const v = r.validation;
              const vr = v.run ? v.passed / v.run : null;
              const b = r.benchmark;
              const bg = (b.MATCH ?? 0) + (b.CLOSE ?? 0);
              const bx = (b.MISMATCH ?? 0) + (b.SCALE_ERROR ?? 0);
              return (
                <tr key={r.stock_code}>
                  <td><Link to={`/company/${r.stock_code}`}>{r.stock_code}</Link></td>
                  <td className="name" title={r.name}><Link to={`/company/${r.stock_code}`}>{r.name}</Link></td>
                  <td className="num">{fmt(r.documents)}</td>
                  <td className="num">{fmt(r.facts)}</td>
                  <td><CoverageCells years={years} statements={r.statements} /></td>
                  <td className={`num ${vr == null ? "muted" : vr === 1 ? "ok" : vr < 0.9 ? "bad" : "warn"}`}>
                    {v.run ? `${v.passed}/${v.run}` : "–"}
                  </td>
                  <td className="num">
                    {bg || bx ? (<><span className="ok">{bg}</span>{bx ? <> / <span className="bad">{bx}</span></> : null}</>)
                      : <span className="muted">–</span>}
                  </td>
                  <td className="muted">{ago(r.last_activity)}</td>
                </tr>
              );
            })}
            {!shown.length && <tr><td colSpan={8} className="empty">No companies match.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>{shown.length} of {rows.length} companies</div>
    </div>
  );
}

// ------------------------------------------------------- manual downloads
const TIER_ORDER: Record<string, number> = { LARGE: 0, MID: 1, SMALL: 2, UNKNOWN: 3 };
const tierRank = (t: string) => TIER_ORDER[t] ?? 9;

/** Only plain http(s) URLs the scraper didn't mark suspect become links. */
function safeUrl(r: ManualDownload): string | null {
  if (r.url_suspect || !r.url) return null;
  try {
    const u = new URL(r.url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

function ManualDownloads({ rows }: { rows: ManualDownload[] | undefined }) {
  const [q, setQ] = useState("");
  const [tier, setTier] = useState("");
  const [pending, setPending] = useState(true);
  const all = useMemo(() => rows ?? [], [rows]);

  const summary = useMemo(() => {
    const by: Record<string, { total: number; pending: number }> = {};
    for (const r of all) {
      by[r.tier] ??= { total: 0, pending: 0 };
      by[r.tier].total++;
      if (!r.documents) by[r.tier].pending++;
    }
    return Object.entries(by).sort(([a], [b]) => tierRank(a) - tierRank(b));
  }, [all]);
  const pendingTotal = all.filter((r) => !r.documents).length;

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all
      .filter((r) => (!pending || !r.documents) && (!tier || r.tier === tier) &&
        (!needle || r.stock_code.toLowerCase().includes(needle) || r.name.toLowerCase().includes(needle)))
      .sort((a, b) => tierRank(a.tier) - tierRank(b.tier) || (b.market_cap_bn ?? -1) - (a.market_cap_bn ?? -1));
  }, [all, q, tier, pending]);

  return (
    <div className="panel">
      <h2>Manual downloads needed</h2>
      {rows === undefined ? (
        <div className="empty">This backend's <code>/api/progress</code> has no <code>manual_downloads</code> field.</div>
      ) : !all.length ? (
        <div className="empty">manual_downloads.txt not found, or nothing needs a manual download.</div>
      ) : (
        <>
          <div className="muted" style={{ marginBottom: 10 }}>
            {fmt(pendingTotal)} pending of {fmt(all.length)}
            {summary.map(([t, v]) => ` · ${t.toLowerCase()} ${v.pending}/${v.total}`).join("")}
          </div>
          <div className="toolbar">
            <input type="search" placeholder="Filter by code or name" value={q} onChange={(e) => setQ(e.target.value)} />
            <select value={tier} onChange={(e) => setTier(e.target.value)}>
              <option value="">All tiers</option>
              {summary.map(([t]) => <option key={t} value={t}>{t}</option>)}
            </select>
            <label className="chk"><input type="checkbox" checked={pending} onChange={(e) => setPending(e.target.checked)} /> Pending only</label>
          </div>
          <div className="tablewrap" style={{ maxHeight: 480, overflowY: "auto" }}>
            <table>
              <thead><tr><th>Tier</th><th>Code</th><th>Name</th><th className="num">MCap (RM bn)</th><th>Why manual</th><th>Status</th><th>IR page</th></tr></thead>
              <tbody>
                {shown.map((r) => {
                  const href = safeUrl(r);
                  return (
                    <tr key={r.stock_code}>
                      <td>{r.tier.toLowerCase()}</td>
                      <td><Link to={`/company/${r.stock_code}`}>{r.stock_code}</Link></td>
                      <td className="name" title={r.name}>{r.name}</td>
                      <td className="num">{r.market_cap_bn != null ? r.market_cap_bn.toFixed(1) : "–"}</td>
                      <td className="muted">{r.reason}</td>
                      <td>
                        {r.documents
                          ? <span className="ok">✓ {r.documents} doc{r.documents > 1 ? "s" : ""}{r.facts ? "" : " · no facts yet"}</span>
                          : <span className="warn">pending</span>}
                      </td>
                      <td className="name">
                        {href
                          ? <a href={href} target="_blank" rel="noopener noreferrer" title={href}>{href.replace(/^https?:\/\/(www\.)?/, "").slice(0, 48)}</a>
                          : <span className="bad" title={r.url ?? ""}>bad URL – search manually</span>}
                      </td>
                    </tr>
                  );
                })}
                {!shown.length && <tr><td colSpan={7} className="empty">Nothing matches.</td></tr>}
              </tbody>
            </table>
          </div>
          <div className="muted" style={{ marginTop: 10, fontSize: 12 }}>
            {shown.length} shown. Save each annual report PDF to <code>pdfs/&lt;stock code&gt;/</code>, then run{" "}
            <code>.venv\Scripts\python -m bursa.cli ingest pdfs/</code> and{" "}
            <code>.venv\Scripts\python -m bursa.cli normalize facts --only-without-facts</code>.
          </div>
        </>
      )}
    </div>
  );
}

function Runs({ d }: { d: Progress }) {
  if (!d.activity.recent.length) return <div className="empty">No runs yet.</div>;
  return (
    <div className="tablewrap">
      <table className="runs">
        <tbody>
          {d.activity.recent.map((r) => (
            <tr key={r.run_id}>
              <td>{r.stock_code ? <Link to={`/company/${r.stock_code}`}>{r.stock_code}</Link> : "–"}</td>
              <td className="name" title={r.document}>{r.document}</td>
              <td className="num">{fmt(r.facts)} facts</td>
              <td className="muted">{ago(r.at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Roadmap({ d }: { d: Progress }) {
  if (!d.roadmap.length) return <div className="empty">No roadmap found in README.md.</div>;
  return (
    <>
      {d.roadmap.map((s) => {
        const done = s.items.filter((i) => i.done).length;
        return (
          <div key={s.section}>
            <div className="rsec"><span>{s.section}</span><span className="muted">{done}/{s.items.length}</span></div>
            <Bar value={pct(done, s.items.length)} />
            <ul className="road">
              {s.items.map((i) => (
                <li key={i.text} className={i.done ? "done" : ""}><InlineCode text={i.text} /></li>
              ))}
            </ul>
          </div>
        );
      })}
    </>
  );
}
