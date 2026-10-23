import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Deadline, type FeedItem, type MonthGroup, type Mover } from "../api";
import { Section } from "../components";
import { money, useAsync, useDebounced } from "../lib";
import { longDate } from "./Brief";

const TONES = [["all", "All"], ["up", "Up"], ["down", "Down"], ["flat", "Flat"]] as const;
type ToneFilter = (typeof TONES)[number][0];

export const signed = (v: number | null | undefined) => (v == null ? "–" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`);
export const rm = (v: number | null | undefined) => (v == null ? "–" : `RM ${money(v)}`);
export const reportLink = (code: string, fy: number) => `/feed/${encodeURIComponent(code)}?fy=${fy}`;

export default function Feed() {
  const feed = useAsync(() => api.feed(), []);
  const [q, setQ] = useState("");
  const [sector, setSector] = useState("");
  const [tone, setTone] = useState<ToneFilter>("all");
  const query = useDebounced(q.trim().toLowerCase());

  const matches = (i: { stock_code: string; name: string }) =>
    !query || i.stock_code.includes(query) || i.name.toLowerCase().includes(query);
  const keep = (i: FeedItem) => matches(i) && (!sector || i.sector === sector);

  return (
    <>
      <div className="pagehead">
        <h1>Results feed</h1>
        {feed.data && (
          <span className="muted">
            Bursa results by financial year end · latest year end on file {feed.data.latest_period_end ?? "–"}
          </span>
        )}
      </div>
      <Section state={feed} isEmpty={(d) => !d.months.length && !d.upcoming.length} empty="No annual facts yet.">
        {(d) => (
          <>
            <Briefs />
            <Upcoming deadlines={d.upcoming.filter(matches)} filtered={!!query} />
            <div className="toolbar" style={{ marginTop: 20 }}>
              <input type="search" placeholder="Filter by company or stock code" value={q} onChange={(e) => setQ(e.target.value)} />
              <select value={sector} onChange={(e) => setSector(e.target.value)}>
                <option value="">All sectors</option>
                {d.sectors.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <div className="seg">
                {TONES.map(([k, l]) => <button key={k} className={tone === k ? "on" : ""} onClick={() => setTone(k)}>{l}</button>)}
              </div>
            </div>
            {d.months.map((m, idx) => {
              const items = m.items.filter((i) => keep(i) && (tone === "all" || i.tone === tone));
              const filtering = !!query || !!sector || tone !== "all";
              if (filtering && !items.length) return null;
              return <Month key={m.month} group={m} items={items} open={idx < 2 || filtering} showDigest={!filtering} />;
            })}
            <p className="muted feed-foot">
              {d.notes.join(" ")} Generated from extracted annual report facts; extraction errors are possible. Not investment advice.
            </p>
          </>
        )}
      </Section>
    </>
  );
}

// ------------------------------------------------------------------ briefs
const BRIEFS_SHOWN = 5;

function Briefs() {
  const briefs = useAsync(() => api.briefs(), []);
  const [all, setAll] = useState(false);
  const list = briefs.data ?? [];
  const shown = all ? list : list.slice(0, BRIEFS_SHOWN);
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <div className="rsec">
        <strong>Morning briefs</strong>
        <span className="muted">Malaysian macro and Bursa corporate news</span>
      </div>
      {!list.length && !briefs.loading && (
        <div className="muted small">
          No briefs yet. <Link to="/feed/brief">Today's filing calendar →</Link>
        </div>
      )}
      {shown.map((b) => (
        <div className="feed-item" key={b.date}>
          <span className="tag annual">Morning</span>
          <span className="feed-text">
            <Link to={`/feed/brief/${b.date}`}>{b.title ?? `Bursa Morning Brief: ${longDate(b.date)}`}</Link>
            {!b.valid && <span className="feed-sum down">invalid file</span>}
          </span>
          <span className="fc" />
        </div>
      ))}
      {list.length > BRIEFS_SHOWN && (
        <button className="chip" style={{ marginTop: 6 }} onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `All ${list.length} briefs`}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- upcoming
const PER_DATE = 12;

function Upcoming({ deadlines, filtered }: { deadlines: Deadline[]; filtered: boolean }) {
  const byDate = useMemo(() => {
    const m = new Map<string, Deadline[]>();
    for (const d of deadlines) (m.get(d.due) ?? m.set(d.due, []).get(d.due)!).push(d);
    return [...m.entries()];
  }, [deadlines]);

  return (
    <div className="panel upcoming" style={{ marginTop: 16 }}>
      <div className="rsec">
        <strong>Upcoming filing deadlines</strong>
        <span className="muted">{deadlines.length} in the next 45 days</span>
      </div>
      {!byDate.length && <div className="empty">{filtered ? "No deadlines for this filter." : "No deadlines in the window."}</div>}
      {byDate.map(([due, list], i) => <DueDate key={due} due={due} list={list} open={i === 0 || filtered} />)}
    </div>
  );
}

function DueDate({ due, list, open }: { due: string; list: Deadline[]; open: boolean }) {
  const [all, setAll] = useState(false);
  const annual = list.filter((d) => d.kind === "annual").length;
  const filed = list.filter((d) => d.on_file).length;
  const shown = all ? list : list.slice(0, PER_DATE);
  return (
    <details className="due" open={open}>
      <summary>
        <span className="due-date">{fmtDate(due)}</span>
        <span className="muted">
          {list.length} due · {annual} annual, {list.length - annual} quarterly{filed ? ` · ${filed} already on file` : ""}
        </span>
      </summary>
      {shown.map((d) => (
        <div className="feed-item" key={`${d.stock_code}-${d.kind}-${d.period_end}`}>
          <span className={`tag ${d.kind}`}>{d.kind === "annual" ? "Annual" : "Quarterly"}</span>
          <span className="feed-text">
            <Link to={`/company/${encodeURIComponent(d.stock_code)}`}>{d.name}</Link> <span className="muted">({d.stock_code})</span> · {d.label}
          </span>
          <span className="fc">{d.on_file ? <span className="pill pass">on file</span> : d.context}</span>
        </div>
      ))}
      {list.length > PER_DATE && (
        <button className="chip" onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${list.length}`}</button>
      )}
    </details>
  );
}

const fmtDate = (iso: string) =>
  new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });

// ------------------------------------------------------------------- month
function Month({ group: g, items, open, showDigest }: { group: MonthGroup; items: FeedItem[]; open: boolean; showDigest: boolean }) {
  return (
    <details className="month" open={open}>
      <summary>
        <h3>{g.label}</h3>
        <span className="muted">{items.length} {items.length === 1 ? "company" : "companies"}</span>
      </summary>
      {showDigest && g.compared > 0 && <Digest g={g} />}
      {items.map((i) => (
        <div className="feed-item" key={`${i.stock_code}-${i.fiscal_year}`}>
          <span className="tag">{i.stock_code}</span>
          <span className="feed-text">
            <Link to={reportLink(i.stock_code, i.fiscal_year)}>{i.name} FY{i.fiscal_year} results</Link>
            <span className={`feed-sum ${i.tone}`}>{i.summary}</span>
          </span>
          <span className="fc">{i.revenue != null ? `rev ${rm(i.revenue)}` : i.patami != null ? `PATAMI ${rm(i.patami)}` : ""}</span>
        </div>
      ))}
    </details>
  );
}

function MoverList({ movers, fmt }: { movers: Mover[]; fmt: (m: Mover) => string }) {
  const shown = movers.slice(0, 6);
  return (
    <>
      {shown.map((m, i) => (
        <span key={m.stock_code}>
          {i > 0 && ", "}
          <Link to={reportLink(m.stock_code, m.fiscal_year)}>{m.name}</Link> {fmt(m)}
        </span>
      ))}
      {movers.length > shown.length && <span className="muted"> and {movers.length - shown.length} more</span>}
    </>
  );
}

function Digest({ g }: { g: MonthGroup }) {
  return (
    <div className="digest">
      <strong>TL;DR</strong>
      <ul>
        <li>
          <strong>{g.compared} companies with a comparable prior year.</strong> Profit rose at {g.up}, fell at {g.down} and was
          flat (within ±5%) at {g.flat}.
        </li>
        {(g.median_revenue_growth != null || g.median_patami_growth != null) && (
          <li>
            Median revenue growth <strong>{signed(g.median_revenue_growth)}</strong>, median PATAMI growth{" "}
            <strong>{signed(g.median_patami_growth)}</strong> (companies profitable in both years).
          </li>
        )}
        {g.gainers.length > 0 && (
          <li><strong>Biggest PATAMI gains</strong> (prior PATAMI ≥ RM 5m): <MoverList movers={g.gainers} fmt={(m) => signed(m.value)} /></li>
        )}
        {g.decliners.length > 0 && (
          <li><strong>Biggest declines:</strong> <MoverList movers={g.decliners} fmt={(m) => signed(m.value)} /></li>
        )}
        {g.turnarounds.length > 0 && (
          <li><strong>Back in profit:</strong> <MoverList movers={g.turnarounds} fmt={(m) => `(${rm(m.value)})`} /></li>
        )}
        {g.to_loss.length > 0 && (
          <li><strong>Swung to a loss:</strong> <MoverList movers={g.to_loss} fmt={(m) => `(${rm(m.value)})`} /></li>
        )}
      </ul>
    </div>
  );
}
