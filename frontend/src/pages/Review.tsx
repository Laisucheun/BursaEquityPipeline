import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api, type ReviewItem } from "../api";
import { ErrorBox, Loading } from "../components";
import { ago, fmt, useAsync } from "../lib";

const PAGE = 25;

/**
 * Concept review queue (ReviewItem rows: unmapped labels, low-confidence facts).
 *
 * The backend does not expose review items yet. This page targets the proposed
 * contract documented in src/api.ts (GET /api/review, POST /api/review/{id}/resolve,
 * GET /api/concepts) and renders an "endpoint not available yet" state on 404.
 */
export default function Review() {
  const [status, setStatus] = useState("open");
  const [reason, setReason] = useState("");
  const [page, setPage] = useState(0);
  const list = useAsync(
    () => api.reviewItems({ status, reason: reason || undefined, limit: PAGE, offset: page * PAGE }),
    [status, reason, page],
  );
  const missing = list.error instanceof ApiError && (list.error.status === 404 || list.error.status === 405);

  return (
    <>
      <div className="pagehead">
        <h1>Concept review</h1>
        {list.data && <span className="muted">{fmt(list.data.total)} items</span>}
      </div>
      <div className="panel" style={{ marginTop: 16 }}>
        {missing ? <NotAvailable /> : (
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
            </div>
            <ErrorBox error={list.error} />
            {list.loading && !list.data && <Loading />}
            {list.data && !list.data.items.length && <div className="empty">Review queue is empty.</div>}
            {list.data?.items.map((it) => <ReviewRow key={it.id} item={it} onResolved={list.reload} />)}
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

function ReviewRow({ item, onResolved }: { item: ReviewItem; onResolved: () => void }) {
  const [concept, setConcept] = useState(item.suggestions?.[0]?.concept_key ?? "");
  const [addSynonym, setAddSynonym] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<Error | null>(null);
  const concepts = useAsync(() => api.concepts(item.statement ?? undefined), [item.statement], !item.resolved_at);

  const resolve = async (action: "map" | "ignore" | "reject") => {
    setBusy(true);
    setErr(null);
    try {
      await api.resolveReview(item.id, { action, concept_key: action === "map" ? concept : undefined, add_synonym: addSynonym });
      onResolved();
    } catch (e) {
      setErr(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="review-item">
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "baseline" }}>
        <span className="pill">{item.reason}</span>
        <strong>{item.raw_label ?? item.detail ?? `Item #${item.id}`}</strong>
        <span className="meta">severity {item.severity}</span>
      </div>
      <div className="meta">
        {item.stock_code ? <Link to={`/company/${item.stock_code}`}>{item.stock_code}</Link> : "–"}
        {item.company_name ? ` ${item.company_name}` : ""} · {item.document_filename ?? `doc #${item.document_id}`}
        {item.page_no != null ? ` · p.${item.page_no}` : ""}{item.statement ? ` · ${item.statement.toUpperCase()}` : ""}
        {" "}· {ago(item.created_at)}
      </div>
      {item.cells?.length ? (
        <div className="meta">Cells: {item.cells.map((c) => c.text).filter(Boolean).join(" | ")}</div>
      ) : null}
      {item.detail && item.raw_label && <div className="meta">{item.detail}</div>}
      {item.resolved_at ? (
        <div className="meta ok">Resolved {ago(item.resolved_at)}{item.resolved_by ? ` by ${item.resolved_by}` : ""}
          {item.resolution ? ` · ${JSON.stringify(item.resolution)}` : ""}</div>
      ) : (
        <div className="toolbar" style={{ margin: 0 }}>
          <input type="text" list={`concepts-${item.id}`} value={concept} onChange={(e) => setConcept(e.target.value)}
            placeholder="concept_key, e.g. is.revenue" style={{ minWidth: 240 }} />
          <datalist id={`concepts-${item.id}`}>
            {item.suggestions?.map((s) => <option key={`s-${s.concept_key}`} value={s.concept_key}>{`${s.label} (${(s.score * 100).toFixed(0)}%)`}</option>)}
            {concepts.data?.map((c) => <option key={c.concept_key} value={c.concept_key}>{c.label}</option>)}
          </datalist>
          <label className="chk"><input type="checkbox" checked={addSynonym} onChange={(e) => setAddSynonym(e.target.checked)} /> Save as synonym</label>
          <button className="primary" disabled={busy || !concept} onClick={() => resolve("map")}>Map</button>
          <button disabled={busy} onClick={() => resolve("ignore")}>Ignore</button>
          <button disabled={busy} onClick={() => resolve("reject")}>Reject</button>
        </div>
      )}
      <ErrorBox error={err} />
    </div>
  );
}

function NotAvailable() {
  return (
    <div className="notice">
      <p style={{ marginTop: 0 }}><strong>Review endpoint not available yet.</strong></p>
      <p>
        The <code>review_items</code> table exists, but the backend doesn't expose it through an API. This page is ready
        and will work as soon as the backend adds these endpoints:
      </p>
      <ul>
        <li><code>GET /api/review?status=open|resolved|all&amp;reason=&amp;stock_code=&amp;limit=&amp;offset=</code> → <code>{"{ total, items: ReviewItem[], reasons: {reason, count}[] }"}</code></li>
        <li><code>POST /api/review/{"{id}"}/resolve</code> with <code>{"{ action: map|ignore|reject, concept_key?, add_synonym?, company_scoped?, resolved_by? }"}</code> → the updated item</li>
        <li><code>GET /api/concepts?statement=is|bs|cf</code> → <code>{"{ concept_key, statement, label }[]"}</code> for the concept picker</li>
      </ul>
      <p style={{ marginBottom: 0 }}>The full field list for <code>ReviewItem</code> is in <code>frontend/src/api.ts</code>.</p>
    </div>
  );
}
