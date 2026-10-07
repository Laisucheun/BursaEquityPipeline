import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { ErrorBox } from "../components";
import { fmt, useAsync, useDebounced } from "../lib";

const PAGE = 50;

export default function Companies() {
  const [params, setParams] = useSearchParams();
  const sector = params.get("sector") ?? "";
  const page = Math.max(0, Number(params.get("page") ?? 0) || 0);
  const [search, setSearch] = useState(params.get("q") ?? "");
  const q = useDebounced(search.trim(), 300);

  const update = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v); else next.delete(k);
    }
    setParams(next, { replace: true });
  };

  // Debounced search goes into the URL and resets to page 0.
  useEffect(() => {
    if (q !== (params.get("q") ?? "")) update({ q: q || null, page: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q]);

  const sectors = useAsync(() => api.sectors(), []);
  const list = useAsync(
    () => api.companies({ search: params.get("q") ?? undefined, sector: sector || undefined, limit: PAGE, offset: page * PAGE }),
    [params.get("q"), sector, page],
  );

  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE));

  return (
    <>
      <div className="pagehead"><h1>Companies</h1><span className="muted">{fmt(total)} companies</span></div>
      <div className="panel" style={{ marginTop: 16 }}>
        <div className="toolbar">
          <input type="search" placeholder="Search by name or stock code" value={search}
            onChange={(e) => setSearch(e.target.value)} />
          <select value={sector} onChange={(e) => update({ sector: e.target.value || null, page: null })}>
            <option value="">All sectors</option>
            {sectors.data?.map((s) => (
              <option key={s.sector} value={s.sector}>{s.sector} ({s.count})</option>
            ))}
          </select>
        </div>
        <ErrorBox error={list.error} />
        <div className="tablewrap" style={{ opacity: list.loading ? 0.6 : 1 }}>
          <table>
            <thead>
              <tr>
                <th>Code</th><th>Name</th><th>Market</th><th>Sector</th>
                <th className="num">FY end</th><th className="num">Documents</th><th>IR site</th>
              </tr>
            </thead>
            <tbody>
              {list.data?.items.map((c) => (
                <tr key={c.id}>
                  <td><Link to={`/company/${c.stock_code}`}>{c.stock_code}</Link></td>
                  <td className="name" title={c.name}><Link to={`/company/${c.stock_code}`}>{c.name}</Link></td>
                  <td>{c.market}</td>
                  <td className="muted">{c.sector ?? "–"}</td>
                  <td className="num">{c.fy_end_month ? monthName(c.fy_end_month) : "–"}</td>
                  <td className={`num ${c.doc_count ? "" : "muted"}`}>{fmt(c.doc_count)}</td>
                  <td className="name">
                    {c.ir_homepage_url
                      ? <a href={c.ir_homepage_url} target="_blank" rel="noreferrer">{hostOf(c.ir_homepage_url)}</a>
                      : <span className="muted">–</span>}
                  </td>
                </tr>
              ))}
              {list.data && !list.data.items.length && (
                <tr><td colSpan={7} className="empty">No companies match.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="pager">
          <span className="muted">Page {page + 1} of {pages}</span>
          <button disabled={page <= 0} onClick={() => update({ page: page - 1 ? String(page - 1) : null })}>Prev</button>
          <button disabled={page + 1 >= pages} onClick={() => update({ page: String(page + 1) })}>Next</button>
        </div>
      </div>
    </>
  );
}

function monthName(m: number) {
  return new Date(2000, m - 1, 1).toLocaleString(undefined, { month: "short" });
}

function hostOf(url: string) {
  try { return new URL(url).host.replace(/^www\./, ""); } catch { return url; }
}
