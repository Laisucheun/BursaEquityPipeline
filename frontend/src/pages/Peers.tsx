import { useSearchParams } from "react-router-dom";
import { useState } from "react";
import { api } from "../api";
import { ErrorBox, Loading, Section } from "../components";
import { useAsync } from "../lib";
import { PeerTable } from "../peers";

/** Sector comparison: every company with facts in one sector, metrics + quartiles. */
export default function Peers() {
  const [params, setParams] = useSearchParams();
  const sector = params.get("sector") ?? "";
  const fy = Number(params.get("fy")) || undefined;
  const [fyInput, setFyInput] = useState(fy ? String(fy) : "");
  const sectors = useAsync(() => api.peerSectors(), []);
  const cmp = useAsync(() => api.sectorPeers(sector, fy), [sector, fy], !!sector);

  const set = (s: string, y?: number) => {
    const p: Record<string, string> = {};
    if (s) p.sector = s;
    if (y) p.fy = String(y);
    setParams(p, { replace: true });
  };

  return (
    <>
      <div className="pagehead">
        <h1>Peers</h1>
        <span className="muted">Facts only, offline. Ratios use each company's latest full fiscal year unless an FY is set.</span>
      </div>
      <div className="panel" style={{ marginTop: 16 }}>
        <ErrorBox error={sectors.error} />
        <div className="toolbar">
          <select value={sector} onChange={(e) => set(e.target.value, fy)} disabled={!sectors.data}>
            <option value="">{sectors.data ? "Choose a sector…" : "Loading sectors…"}</option>
            {sectors.data?.map((s) => (
              <option key={s.sector} value={s.sector}>{s.sector} ({s.with_facts}/{s.companies} with facts)</option>
            ))}
          </select>
          <form style={{ display: "flex", gap: 8 }} onSubmit={(e) => {
            e.preventDefault();
            set(sector, /^\d{4}$/.test(fyInput.trim()) ? Number(fyInput.trim()) : undefined);
          }}>
            <input type="text" placeholder="FY (latest)" value={fyInput} onChange={(e) => setFyInput(e.target.value)} style={{ width: 110 }} />
            <button type="submit" disabled={!sector}>Apply</button>
          </form>
          {cmp.loading && cmp.data && <Loading />}
        </div>
        {!sector ? (
          <div className="empty">Pick a sector to compare its companies.</div>
        ) : (
          <Section state={cmp} isEmpty={(d) => !d.rows.length} empty="No companies with facts in this sector.">
            {(d) => <PeerTable data={d} />}
          </Section>
        )}
      </div>
    </>
  );
}
