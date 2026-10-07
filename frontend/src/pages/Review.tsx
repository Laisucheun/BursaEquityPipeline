import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, type ReviewItem, type ReviewResolveRequest } from "../api";
import { ErrorBox, Loading } from "../components";
import { ago, fmt, useAsync, useDebounced } from "../lib";

const PAGE = 25;
const REVIEWER_KEY = "bursa.reviewer";

type Resolver = (id: number, body: ReviewResolveRequest) => Promise<ReviewItem>;

function loadReviewer(): string {
  try { return localStorage.getItem(REVIEWER_KEY) ?? ""; } catch { return ""; }
}
function saveReviewer(v: string) {
  try { localStorage.setItem(REVIEWER_KEY, v); } catch { /* storage blocked */ }
}

/**
 * Concept review queue (src/bursa/api/routes/review.py): unmapped, figure-bearing
 * statement rows raised during `normalize`. Map one to a concept (optionally saving
 * the label as a synonym), ignore it, or reject it as not a line item.
 */
export default function Review() {
  const [status, setStatus] = useState("open");
  const [reason, setReason] = useState("");
  const [stock, setStock] = useState("");
  const [page, setPage] = useState(0);
  const [reviewer, setReviewer] = useState(loadReviewer);
  const [demo, setDemo] = useState(false);
  const stockQ = useDebounced(stock.trim(), 300);
  const list = useAsync(
    () => api.reviewItems({ status, reason: reason || undefined, stock_code: stockQ || undefined, limit: PAGE, offset: page * PAGE }),
    [status, reason, stockQ, page],
  );
  const missing = list.error instanceof ApiError && (list.error.status === 404 || list.error.status === 405);
  const filtered = !!(reason || stockQ) || status !== "open";

  return (
    <>
      <div className="pagehead">
        <h1>Concept review</h1>
        {list.data && <span className="muted">{fmt(list.data.total)} {status === "all" ? "" : status} item{list.data.total === 1 ? "" : "s"}</span>}
      </div>
      <div className="panel" style={{ marginTop: 16 }}>
        {missing ? (
          <div className="notice">
            <strong>The backend has no <code>/api/review</code> endpoint.</strong> Restart <code>bursa serve</code> on the current code.
          </div>
        ) : (
          <>
            <div className="toolbar">
              <div className="seg">
                {["open", "resolved", "all"].map((s) => (
                  <button key={s} className={status === s ? "on" : ""} onClick={() => { setStatus(s); setPage(0); }}>{s}</button>
                ))}
              </div>
              <select value={reason} onChange={(e) => { setReason(e.target.value); setPage(0); }}>
                <option value="">All reasons</option>
                {list.data?.reasons.map((r) => <option key={r.reason} value={r.reason}>{r.reason} ({r.count})</option>)}
              </select>
              <input type="text" placeholder="Stock code" value={stock} style={{ width: 120 }}
                onChange={(e) => { setStock(e.target.value); setPage(0); }} />
              <input type="text" placeholder="Reviewer name (optional)" value={reviewer} style={{ width: 200 }}
                onChange={(e) => { setReviewer(e.target.value); saveReviewer(e.target.value); }} />
              <button onClick={list.reload} disabled={list.loading}>{list.loading ? "Loading…" : "Refresh"}</button>
            </div>
            <ErrorBox error={list.error} />
            {list.loading && !list.data && <Loading />}
            {list.data && !list.data.items.length && !demo && (
              filtered ? <div className="empty">No items match these filters.</div> : <EmptyQueue onDemo={() => setDemo(true)} />
            )}
            {demo && !list.data?.items.length && <DemoQueue reviewer={reviewer} onClose={() => setDemo(false)} />}
            {list.data?.items.map((it) => (
              <ReviewRow key={`${it.id}-${it.resolved_at ?? ""}`} item={it} reviewer={reviewer} resolver={api.resolveReview} onResolved={list.reload} />
            ))}
            {list.data && list.data.total > PAGE && (
              <div className="pager">
                <span className="muted">Page {page + 1} of {Math.ceil(list.data.total / PAGE)}</span>
                <button disabled={page === 0} onClick={() => setPage(page - 1)}>Prev</button>
                <button disabled={(page + 1) * PAGE >= list.data.total} onClick={() => setPage(page + 1)}>Next</button>
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}

function EmptyQueue({ onDemo }: { onDemo: () => void }) {
  return (
    <div className="notice">
      <p style={{ marginTop: 0 }}><strong>The review queue is empty.</strong></p>
      <p>
        Items are raised during normalization: every unmapped income-statement, balance-sheet or cash-flow row that
        carries figures becomes one open item per label. Run <code>.venv\Scripts\python -m bursa.cli normalize facts</code> and
        refresh this page.
      </p>
      <p style={{ marginBottom: 0 }}>
        <button onClick={onDemo}>Show a sample item</button>{" "}
        <span className="meta">Local preview only. Nothing is sent to the backend.</span>
      </p>
    </div>
  );
}

const SAMPLE: ReviewItem = {
  id: -1, run_id: 0, document_id: 0, document_filename: "sample-annual-report.pdf",
  stock_code: null, company_name: "Sample Berhad", raw_row_id: null, fact_id: null,
  reason: "UNMAPPED_LABEL", detail: null, severity: 62, created_at: new Date().toISOString(),
  resolved_at: null, resolved_by: null, resolution: null,
  raw_label: "Revenue from contracts with customers", page_no: 84, statement: "is",
  cells: [{ col_index: 2, text: "1419626" }, { col_index: 3, text: "1203511" }],
  suggestions: [{ concept_key: "is.revenue", label: "Revenue", score: 0.5 }],
};

/** Sample item with a mocked resolver, so the workflow can be tried on an empty queue. */
function DemoQueue({ reviewer, onClose }: { reviewer: string; onClose: () => void }) {
  const [item, setItem] = useState(SAMPLE);
  const mock: Resolver = async (_id, body) => {
    await new Promise((r) => setTimeout(r, 300));
    if (body.action === "map" && !body.concept_key) throw new ApiError(422, "concept_key is required when action is 'map'");
    return {
      ...item,
      resolved_at: new Date().toISOString(),
      resolved_by: body.resolved_by || "reviewer",
      resolution: body.action === "map"
        ? { action: "map", concept_key: body.concept_key, ...(body.add_synonym ? { synonym: { normalized: "revenue from contracts with customers", company_scoped: !!body.company_scoped } } : {}) }
        : body.action === "reject" ? { action: "reject", note: "not a line item" } : { action: "ignore" },
    };
  };
  return (
    <div style={{ marginTop: 12 }}>
      <div className="toolbar" style={{ justifyContent: "space-between" }}>
        <span className="pill CLOSE">sample, not saved</span>
        <span>
          <button onClick={() => setItem(SAMPLE)}>Reset</button>{" "}
          <button onClick={onClose}>Close</button>
        </span>
      </div>
      <ReviewRow key={`${item.resolved_at ?? ""}`} item={item} reviewer={reviewer} resolver={mock} onResolved={() => {}} onResult={setItem} />
    </div>
  );
}

function ReviewRow({ item, reviewer, resolver, onResolved, onResult }: {
  item: ReviewItem;
  reviewer: string;
  resolver: Resolver;
  onResolved: () => void;
  onResult?: (it: ReviewItem) => void;
}) {
  const [concept, setConcept] = useState(item.suggestions?.[0]?.concept_key ?? "");
  const [addSynonym, setAddSynonym] = useState(true);
  const [companyScoped, setCompanyScoped] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<Error | null>(null);
  const [done, setDone] = useState<ReviewItem | null>(null);
  const concepts = useAsync(() => api.concepts(item.statement ?? undefined), [item.statement], !item.resolved_at);
  const known = concepts.data ? new Set(concepts.data.map((c) => c.concept_key)) : null;
  const unknownConcept = !!concept && !!known && !known.has(concept);

  const resolve = async (action: "map" | "ignore" | "reject") => {
    setBusy(true);
    setErr(null);
    try {
      const body: ReviewResolveRequest = { action, resolved_by: reviewer.trim() || undefined };
      if (action === "map") Object.assign(body, { concept_key: concept.trim(), add_synonym: addSynonym, company_scoped: addSynonym && companyScoped });
      const updated = await resolver(item.id, body);
      setDone(updated);
      onResult?.(updated);
      onResolved();
    } catch (e) {
      setErr(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setBusy(false);
    }
  };

  const shown = done ?? item;
  const res = shown.resolution;

  return (
    <div className="review-item">
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "baseline" }}>
        <span className="pill">{shown.reason}</span>
        {shown.statement && <span className="pill MISSING">{shown.statement.toUpperCase()}</span>}
        <strong>{shown.raw_label ?? `Item #${shown.id}`}</strong>
        <span className="meta" title="0-100: larger figures and labels seen at many issuers rank first">severity {shown.severity}</span>
      </div>
      <div className="meta">
        {shown.stock_code ? <Link to={`/company/${shown.stock_code}`}>{shown.stock_code}</Link> : "–"}
        {shown.company_name ? ` ${shown.company_name}` : ""} · {shown.document_filename ?? `doc #${shown.document_id}`}
        {shown.page_no != null ? ` · p.${shown.page_no}` : ""} · raised {ago(shown.created_at)}
      </div>
      {shown.cells?.length ? (
        <div className="meta">Figures: {shown.cells.map((c) => c.text).filter(Boolean).join(" | ")}</div>
      ) : null}
      {shown.resolved_at ? (
        <div className="meta ok">
          Resolved {ago(shown.resolved_at)}{shown.resolved_by ? ` by ${shown.resolved_by}` : ""}
          {res ? ` · ${res.action}` : ""}
          {res?.concept_key ? <> → <code>{res.concept_key}</code></> : null}
          {res?.synonym ? ` · synonym saved${res.synonym.company_scoped ? " (this company only)" : " (all companies)"}` : ""}
          {res?.note ? ` · ${res.note}` : ""}
        </div>
      ) : (
        <>
          {item.suggestions?.length ? (
            <div className="meta">
              Suggestions:{" "}
              {item.suggestions.map((s) => (
                <button key={s.concept_key} className="chip" onClick={() => setConcept(s.concept_key)} title={s.concept_key}>
                  {s.label} <span className="muted">{(s.score * 100).toFixed(0)}%</span>
                </button>
              ))}
            </div>
          ) : null}
          <div className="toolbar" style={{ margin: 0 }}>
            <input type="text" list={`concepts-${item.id}`} value={concept} onChange={(e) => setConcept(e.target.value)}
              placeholder={`concept_key, e.g. ${item.statement ?? "is"}.revenue`} style={{ minWidth: 240 }} />
            <datalist id={`concepts-${item.id}`}>
              {concepts.data?.map((c) => <option key={c.concept_key} value={c.concept_key}>{c.label}</option>)}
            </datalist>
            <label className="chk"><input type="checkbox" checked={addSynonym} onChange={(e) => setAddSynonym(e.target.checked)} /> Save as synonym</label>
            <label className="chk" title="Synonym applies only to this issuer">
              <input type="checkbox" checked={companyScoped} disabled={!addSynonym} onChange={(e) => setCompanyScoped(e.target.checked)} /> This company only
            </label>
            <button className="primary" disabled={busy || !concept.trim() || unknownConcept} onClick={() => resolve("map")}>Map</button>
            <button disabled={busy} onClick={() => resolve("ignore")} title="Real line item, but not worth a concept">Ignore</button>
            <button disabled={busy} onClick={() => resolve("reject")} title="Not a line item (header, note text, mis-read)">Reject</button>
          </div>
          {unknownConcept && <div className="meta warn">Unknown concept for this statement.</div>}
        </>
      )}
      <ErrorBox error={err} />
    </div>
  );
}
