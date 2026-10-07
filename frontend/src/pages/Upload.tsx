import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, uploadPdf, type CompanySummary, type UploadResult } from "../api";
import { Bar } from "../components";
import { fmt, useAsync, useDebounced } from "../lib";

type Phase = "idle" | "uploading" | "processing" | "done" | "error";

export default function Upload() {
  const [file, setFile] = useState<File | null>(null);
  const [code, setCode] = useState("");
  const [over, setOver] = useState(false);
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<UploadResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [startedAt, setStartedAt] = useState<number>(0);
  const abortRef = useRef<(() => void) | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Company picker: search /api/companies as the user types.
  const q = useDebounced(code.trim(), 250);
  const matches = useAsync(() => api.companies({ search: q, limit: 8 }), [q], q.length >= 2);
  const exact: CompanySummary | undefined = matches.data?.items.find((c) => c.stock_code === code.trim());

  const pick = (f: File | undefined | null) => {
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".pdf")) {
      setError("Only PDF files are accepted.");
      return;
    }
    setError(null);
    setFile(f);
  };

  const busy = phase === "uploading" || phase === "processing";

  const submit = async () => {
    if (!file || !code.trim()) return;
    setPhase("uploading");
    setProgress(0);
    setResult(null);
    setError(null);
    setStartedAt(Date.now());
    const { promise, abort } = uploadPdf(file, code.trim(), (f) => {
      setProgress(f);
      if (f >= 1) setPhase("processing");
    });
    abortRef.current = abort;
    try {
      setResult(await promise);
      setPhase("done");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    } finally {
      abortRef.current = null;
    }
  };

  return (
    <>
      <div className="pagehead"><h1>Upload annual report</h1></div>
      <section className="grid two">
        <div className="panel">
          <h2>PDF</h2>
          <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
            <label className="field">
              <span>Stock code</span>
              <input type="text" value={code} onChange={(e) => setCode(e.target.value)} placeholder="e.g. 5101" list="co-matches"
                disabled={busy} autoComplete="off" />
              <datalist id="co-matches">
                {matches.data?.items.map((c) => <option key={c.id} value={c.stock_code}>{c.name}</option>)}
              </datalist>
              <span className="muted" style={{ fontWeight: 400 }}>
                {exact ? exact.name : q.length >= 2 && matches.data && !matches.data.items.length ? "No matching company" : "Type a code or name to search"}
              </span>
            </label>
            <div
              className={`drop${over ? " over" : ""}`}
              onClick={() => !busy && inputRef.current?.click()}
              onDragOver={(e) => { e.preventDefault(); setOver(true); }}
              onDragLeave={() => setOver(false)}
              onDrop={(e) => { e.preventDefault(); setOver(false); if (!busy) pick(e.dataTransfer.files[0]); }}
            >
              {file ? (
                <><strong>{file.name}</strong><br />{(file.size / 1e6).toFixed(1)} MB · click to change</>
              ) : (
                <><strong>Drop a PDF here</strong> or click to choose</>
              )}
              <input ref={inputRef} type="file" accept="application/pdf,.pdf" hidden
                onChange={(e) => pick(e.target.files?.[0])} />
            </div>
            <div className="toolbar" style={{ marginTop: 14 }}>
              <button className="primary" type="submit" disabled={!file || !code.trim() || busy}>
                {busy ? "Working…" : "Upload & run pipeline"}
              </button>
              {phase === "uploading" && <button type="button" onClick={() => abortRef.current?.()}>Cancel</button>}
            </div>
          </form>
          <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
            The server ingests the file, extracts statements, normalizes and derives facts, then validates.
            A large report can take a minute or more after the upload finishes.
          </p>
        </div>

        <div className="panel">
          <h2>Result</h2>
          {phase === "idle" && !error && <div className="empty">Nothing uploaded yet.</div>}
          {phase === "uploading" && (
            <>
              <div className="meta">Uploading… {(progress * 100).toFixed(0)}%</div>
              <Bar value={progress * 100} big />
            </>
          )}
          {phase === "processing" && (
            <>
              <div className="meta">Upload complete. Running extraction pipeline…</div>
              <div className="bar big indeterminate"><span /></div>
              <Elapsed since={startedAt} />
            </>
          )}
          {error && <div className="err-box">{error}</div>}
          {phase === "done" && result && <ResultView r={result} code={code.trim()} />}
        </div>
      </section>
    </>
  );
}

function Elapsed({ since }: { since: number }) {
  const [, force] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => force((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, []);
  return <div className="meta" style={{ marginTop: 6 }}>{Math.round((Date.now() - since) / 1000)}s elapsed</div>;
}

function ResultView({ r, code }: { r: UploadResult; code: string }) {
  const v = r.validation;
  return (
    <>
      <dl className="dl">
        <dt>Document</dt><dd>#{r.document_id} {r.created ? "(new)" : <span className="warn">(already ingested – same file hash)</span>}</dd>
        <dt>Pages</dt><dd>{r.page_count ?? "–"}</dd>
        <dt>Financial statements</dt>
        <dd className={r.has_financial_statements ? "ok" : "bad"}>{r.has_financial_statements ? "detected" : "not detected"}</dd>
        <dt>Statements found</dt><dd>{r.statements_found.length ? r.statements_found.join(", ") : <span className="muted">none</span>}</dd>
        <dt>Facts</dt><dd>{fmt(r.facts_written)} written · {fmt(r.facts_updated)} updated · {fmt(r.facts_derived)} derived</dd>
        <dt>Periods created</dt><dd>{fmt(r.periods_created)}</dd>
        <dt>Validation</dt>
        <dd className={v.rules_failed ? "warn" : "ok"}>{v.rules_passed}/{v.rules_run} passed</dd>
        {r.comparative && (
          <>
            <dt>Comparatives</dt>
            <dd>{r.comparative.match} match · {r.comparative.rounding} rounding · <span className={r.comparative.restatement ? "warn" : ""}>{r.comparative.restatement} restatement</span></dd>
          </>
        )}
      </dl>
      {v.failures.length > 0 && (
        <>
          <h3>Failed rules</h3>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            {v.failures.map((f, i) => <li key={i}><code>{f.rule}</code> <span className="muted">{f.detail}</span></li>)}
          </ul>
        </>
      )}
      <p><Link to={`/company/${code}?tab=facts`}>View facts for {code}</Link></p>
    </>
  );
}
