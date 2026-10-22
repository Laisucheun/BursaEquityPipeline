import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type FinalizeResult, type IngestResult } from "../api";
import { Bar } from "../components";
import { dur, useAsync } from "../lib";

type FileStatus = "queued" | "uploading" | "ingested" | "duplicate" | "error";
type CodeSource = "folder" | "filename" | "default" | "manual" | "none";

interface Row {
  id: number;
  file: File;
  path: string;
  code: string;
  source: CodeSource;
  status: FileStatus;
  result?: IngestResult;
  error?: string;
}

type CompanyStatus = "waiting" | "running" | "done" | "error" | "skipped";

interface CompanyRun {
  code: string;
  status: CompanyStatus;
  result?: FinalizeResult;
  error?: string;
}

const YEAR = /(?:^|\D)(19[89]\d|20[0-4]\d)(?=\D|$)/;
const FOUR = /(?:^|\D)(\d{4})(?=\D|$)/g;

/** Same rule as bursa.pipeline.ingest.parse_filename: first 4-digit token that isn't the year. */
export function codeFromFilename(name: string): string | null {
  const stem = name.replace(/\.pdf$/i, "");
  const year = YEAR.exec(stem)?.[1];
  for (const m of stem.matchAll(FOUR)) if (m[1] !== year) return m[1];
  return null;
}

/**
 * Nearest enclosing folder that is a real stock code wins, then a real code in
 * the filename. Only then unknown 4-digit names (flagged in the UI), skipping
 * year-like folders so pdfs/2024/5101_AR_2024.pdf is 5101, not "2024".
 */
export function detectCode(path: string, known: Set<string>): { code: string; source: CodeSource } {
  const parts = path.split("/").filter(Boolean);
  const folders = parts.slice(0, -1).reverse().filter((p) => /^\d{4}$/.test(p));
  const fromName = codeFromFilename(parts[parts.length - 1] ?? "");
  const knownFolder = folders.find((p) => known.has(p));
  if (knownFolder) return { code: knownFolder, source: "folder" };
  if (fromName && known.has(fromName)) return { code: fromName, source: "filename" };
  const folder = folders.find((p) => !YEAR.test(p));
  if (folder) return { code: folder, source: "folder" };
  if (fromName) return { code: fromName, source: "filename" };
  return { code: "", source: "none" };
}

// --------------------------------------------------- reading dropped folders
interface Picked { file: File; path: string }

type Entry = FileSystemEntry;
const readAll = (dir: FileSystemDirectoryEntry) =>
  new Promise<Entry[]>((resolve, reject) => {
    const reader = dir.createReader();
    const out: Entry[] = [];
    // readEntries returns at most ~100 entries per call; keep reading until empty.
    const next = () => reader.readEntries((batch) => {
      if (!batch.length) resolve(out);
      else { out.push(...batch); next(); }
    }, reject);
    next();
  });

async function walk(entry: Entry, out: Picked[]): Promise<void> {
  if (entry.isFile) {
    const file = await new Promise<File>((res, rej) => (entry as FileSystemFileEntry).file(res, rej));
    out.push({ file, path: entry.fullPath.replace(/^\//, "") });
  } else if (entry.isDirectory) {
    for (const child of await readAll(entry as FileSystemDirectoryEntry)) await walk(child, out);
  }
}

async function filesFromDrop(dt: DataTransfer): Promise<Picked[]> {
  // Entries must be taken synchronously: the DataTransfer is emptied once the handler yields.
  const entries = [...dt.items].map((i) => i.webkitGetAsEntry?.()).filter((e): e is Entry => !!e);
  if (!entries.length) return [...dt.files].map((file) => ({ file, path: file.name }));
  const out: Picked[] = [];
  for (const e of entries) await walk(e, out);
  return out;
}

// ------------------------------------------------------------------- page
export default function BulkUpload() {
  const companies = useAsync(() => api.companies({ limit: 2000 }), []);
  const names = useMemo(
    () => new Map((companies.data?.items ?? []).map((c) => [c.stock_code, c.name])),
    [companies.data],
  );
  const known = useMemo(() => new Set(names.keys()), [names]);

  const [rows, setRows] = useState<Row[]>([]);
  const [runs, setRuns] = useState<CompanyRun[]>([]);
  const [fallback, setFallback] = useState("");
  const [finalize, setFinalize] = useState(true);
  const [running, setRunning] = useState(false);
  const [ignored, setIgnored] = useState(0);
  const [over, setOver] = useState(false);
  const [started, setStarted] = useState(0);
  const [, tick] = useState(0);
  const stopRef = useRef(false);
  const [runIds, setRunIds] = useState<Set<number>>(new Set());
  const nextId = useRef(1);
  const filesRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    folderRef.current?.setAttribute("webkitdirectory", "");
  }, []);

  useEffect(() => {
    setRows((prev) => prev.map((r) =>
      r.status === "queued" && r.source !== "manual" ? { ...r, ...detectCode(r.path, known) } : r));
  }, [known]);

  useEffect(() => {
    if (!running) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    window.addEventListener("beforeunload", warn);
    return () => { window.removeEventListener("beforeunload", warn); window.clearInterval(t); };
  }, [running]);

  const add = (picked: Picked[]) => {
    const pdfs = picked.filter((p) => p.file.name.toLowerCase().endsWith(".pdf"));
    setIgnored((n) => n + picked.length - pdfs.length);
    setRows((prev) => {
      const seen = new Set(prev.map((r) => `${r.path}|${r.file.size}`));
      const fresh = pdfs
        .filter((p) => !seen.has(`${p.path}|${p.file.size}`))
        .map((p) => ({ id: nextId.current++, file: p.file, path: p.path, ...detectCode(p.path, known), status: "queued" as const }));
      return [...prev, ...fresh];
    });
  };

  const fromInput = (list: FileList | null) => {
    if (!list) return;
    add([...list].map((file) => ({ file, path: file.webkitRelativePath || file.name })));
  };

  const effective = (r: Row) => r.code || fallback.trim();
  const valid = (r: Row) => known.has(effective(r));
  const update = (id: number, patch: Partial<Row>) =>
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));

  const queued = rows.filter((r) => r.status === "queued" || r.status === "error");
  const ready = queued.filter(valid);

  const start = async () => {
    stopRef.current = false;
    setRunning(true);
    setStarted(Date.now());
    const todo = [...ready].sort((a, b) => effective(a).localeCompare(effective(b)) || a.path.localeCompare(b.path));
    setRunIds(new Set(todo.map((r) => r.id)));
    const byCompany = new Map<string, Row[]>();
    for (const r of todo) (byCompany.get(effective(r)) ?? byCompany.set(effective(r), []).get(effective(r))!).push(r);
    setRuns([...byCompany.keys()].map((code) => ({ code, status: "waiting" })));
    const setRun = (code: string, patch: Partial<CompanyRun>) =>
      setRuns((prev) => prev.map((c) => (c.code === code ? { ...c, ...patch } : c)));

    for (const [code, files] of byCompany) {
      if (stopRef.current) break;
      let created = 0;
      for (const r of files) {
        if (stopRef.current) break;
        update(r.id, { status: "uploading", error: undefined });
        try {
          const res = await api.ingestPdf(r.file, code);
          if (res.created) created++;
          update(r.id, { status: res.created ? "ingested" : "duplicate", result: res });
        } catch (e) {
          update(r.id, { status: "error", error: e instanceof Error ? e.message : String(e) });
        }
      }
      if (!finalize || stopRef.current || !created) {
        setRun(code, { status: "skipped", error: !created ? "no new files" : undefined });
        continue;
      }
      setRun(code, { status: "running" });
      try {
        setRun(code, { status: "done", result: await api.finalizeCompany(code) });
      } catch (e) {
        setRun(code, { status: "error", error: e instanceof Error ? e.message : String(e) });
      }
    }
    setRunning(false);
  };

  const counts = rows.reduce<Record<string, number>>((m, r) => ({ ...m, [r.status]: (m[r.status] ?? 0) + 1 }), {});
  const inRun = rows.filter((r) => runIds.has(r.id));
  const doneThisRun = inRun.filter((r) => r.status !== "queued" && r.status !== "uploading").length;
  const elapsed = running ? (Date.now() - started) / 1000 : 0;
  const left = inRun.length - doneThisRun;
  const companiesInQueue = new Set(rows.map(effective).filter(Boolean)).size;
  const unknownCodes = rows.filter((r) => r.status === "queued" && !valid(r)).length;

  return (
    <>
      <section className="grid two">
        <div className="panel">
          <h2>Files</h2>
          <div
            className={`drop${over ? " over" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setOver(true); }}
            onDragLeave={() => setOver(false)}
            onDrop={async (e) => {
              e.preventDefault();
              setOver(false);
              if (!running) add(await filesFromDrop(e.dataTransfer));
            }}
          >
            <strong>Drop PDFs or whole folders here</strong><br />
            Nested folders are read recursively.
            <div className="toolbar" style={{ justifyContent: "center", marginTop: 12, marginBottom: 0 }}>
              <button type="button" disabled={running} onClick={() => filesRef.current?.click()}>Choose files</button>
              <button type="button" disabled={running} onClick={() => folderRef.current?.click()}>Choose folder</button>
            </div>
            <input ref={filesRef} type="file" accept="application/pdf,.pdf" multiple hidden
              onChange={(e) => { fromInput(e.target.files); e.target.value = ""; }} />
            <input ref={folderRef} type="file" multiple hidden
              onChange={(e) => { fromInput(e.target.files); e.target.value = ""; }} />
          </div>
          <label className="field" style={{ marginTop: 14 }}>
            <span>Fallback stock code (for files with no code in their folder or name)</span>
            <input type="text" value={fallback} onChange={(e) => setFallback(e.target.value)} placeholder="optional, e.g. 5101"
              disabled={running} />
            {fallback.trim() && <span className="muted" style={{ fontWeight: 400 }}>{names.get(fallback.trim()) ?? "No such company"}</span>}
          </label>
          <label className="chk">
            <input type="checkbox" checked={finalize} onChange={(e) => setFinalize(e.target.checked)} disabled={running} />
            Run normalize → derive → validate once per company after its files are in
          </label>
          <div className="toolbar" style={{ marginTop: 14 }}>
            <button className="primary" disabled={running || !ready.length || companies.loading} onClick={start}>
              {running ? "Working…" : `Upload ${ready.length} file${ready.length === 1 ? "" : "s"}`}
            </button>
            {running && <button onClick={() => { stopRef.current = true; }}>Stop after current file</button>}
            {!running && rows.length > 0 && (
              <button onClick={() => { setRows([]); setRuns([]); setIgnored(0); setRunIds(new Set()); }}>Clear list</button>
            )}
          </div>
          <p className="muted small" style={{ marginBottom: 0 }}>
            Company is taken from the nearest folder named like a stock code (<code>pdfs/5101/…</code>), else a
            4-digit code in the filename (<code>5101_AR_2025.pdf</code>). Re-uploading an identical file is detected and skipped.
          </p>
        </div>

        <div className="panel">
          <h2>Progress</h2>
          {!rows.length ? <div className="empty">No files selected.</div> : (
            <>
              <dl className="dl">
                <dt>Files</dt><dd>{rows.length} PDFs across {companiesInQueue} companies{ignored ? ` · ${ignored} non-PDF ignored` : ""}</dd>
                <dt>Ready</dt><dd>{ready.length}{unknownCodes ? <span className="warn"> · {unknownCodes} need a valid stock code</span> : ""}</dd>
                <dt>Ingested</dt><dd>{counts.ingested ?? 0} new · {counts.duplicate ?? 0} already in system{counts.error ? <span className="bad"> · {counts.error} failed</span> : ""}</dd>
                {runs.length > 0 && (
                  <><dt>Companies</dt>
                    <dd>{runs.filter((c) => c.status === "done").length}/{runs.length} finalized
                      {runs.some((c) => c.status === "error") && <span className="bad"> · {runs.filter((c) => c.status === "error").length} failed</span>}</dd></>
                )}
                {running && (
                  <><dt>Elapsed</dt>
                    <dd>{dur(elapsed)} · {doneThisRun}/{inRun.length} files{doneThisRun > 0 && left > 0 ? ` · ~${dur((elapsed / doneThisRun) * left)} left` : ""}</dd></>
                )}
              </dl>
              {inRun.length > 0 && <Bar value={(100 * doneThisRun) / inRun.length} big style={{ marginTop: 12 }} />}
            </>
          )}
          {runs.length > 0 && <CompanyTable runs={runs} names={names} />}
        </div>
      </section>

      {rows.length > 0 && (
        <div className="panel" style={{ marginTop: 16 }}>
          <h2>Queue</h2>
          <div className="tablewrap" style={{ maxHeight: 520, overflowY: "auto" }}>
            <table>
              <thead><tr><th>File</th><th>Stock code</th><th>Company</th><th>Status</th><th>Detail</th><th /></tr></thead>
              <tbody>
                {rows.map((r) => {
                  const code = effective(r);
                  const ok = known.has(code);
                  const editable = !running && (r.status === "queued" || r.status === "error");
                  return (
                    <tr key={r.id}>
                      <td className="name" title={r.path}>{r.path}</td>
                      <td>
                        <input type="text" value={r.code} placeholder={fallback.trim() || "code"} disabled={!editable}
                          className={ok ? "" : "bad-input"} style={{ width: 72 }}
                          onChange={(e) => update(r.id, { code: e.target.value.trim(), source: "manual" })} />
                        <span className="muted small"> {r.code ? r.source : fallback.trim() ? "default" : ""}</span>
                      </td>
                      <td className="name">{ok ? names.get(code) : <span className="bad">{code ? "unknown code" : "no code"}</span>}</td>
                      <td><StatusPill r={r} /></td>
                      <td className="muted wrap-text small">
                        {r.error ?? (r.result
                          ? `${r.result.page_count ?? "?"} pages · ${r.result.doc_type.toLowerCase().replace("_", " ")}`
                            + (r.result.has_financial_statements ? "" : " · no financial statements detected")
                          : "")}
                        {r.result && r.result.stock_code !== code && (
                          <span className="warn"> · already filed under {r.result.stock_code ?? "no company"} {r.result.company ?? ""}</span>
                        )}
                      </td>
                      <td>{editable && <button className="chip" onClick={() => setRows((p) => p.filter((x) => x.id !== r.id))}>remove</button>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}

function StatusPill({ r }: { r: Row }) {
  if (r.status === "uploading") return <span className="pill RUNNING"><span className="spin" />uploading</span>;
  if (r.status === "ingested") return <span className={`pill ${r.result?.has_financial_statements ? "pass" : "CLOSE"}`}>ingested</span>;
  if (r.status === "duplicate") return <span className="pill MISSING">already in system</span>;
  if (r.status === "error") return <span className="pill fail">failed</span>;
  return <span className="pill MISSING">queued</span>;
}

function CompanyTable({ runs, names }: { runs: CompanyRun[]; names: Map<string, string> }) {
  return (
    <div className="tablewrap" style={{ maxHeight: 320, overflowY: "auto", marginTop: 14 }}>
      <table>
        <thead><tr><th>Company</th><th>Pipeline</th><th className="num">Facts</th><th className="num">Validation</th></tr></thead>
        <tbody>
          {runs.map((c) => (
            <tr key={c.code}>
              <td className="name"><Link to={`/company/${c.code}?tab=facts`}>{c.code}</Link> <span className="muted">{names.get(c.code)}</span></td>
              <td>
                {c.status === "running" ? <span className="pill RUNNING"><span className="spin" />running</span>
                  : c.status === "done" ? <span className="pill pass">done</span>
                  : c.status === "error" ? <span className="pill fail" title={c.error}>failed</span>
                  : c.status === "skipped" ? <span className="pill MISSING" title={c.error}>{c.error ?? "skipped"}</span>
                  : <span className="pill MISSING">waiting</span>}
              </td>
              <td className="num">{c.result ? `${c.result.facts_written} new · ${c.result.facts_updated} upd` : "–"}</td>
              <td className={`num ${c.result?.validation.rules_failed ? "warn" : ""}`}>
                {c.result ? `${c.result.validation.rules_passed}/${c.result.validation.rules_run}` : "–"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
