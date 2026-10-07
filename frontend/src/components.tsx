import type { CSSProperties, ReactNode } from "react";
import type { StatementPrefix } from "./api";

export function Bar({ value, big, style }: { value: number; big?: boolean; style?: CSSProperties }) {
  return (
    <div className={`bar${big ? " big" : ""}`} style={style}>
      <span style={{ width: `${Math.max(0, Math.min(100, value)).toFixed(1)}%` }} />
    </div>
  );
}

/** Render `backtick` spans as <code>, everything else as plain text. */
export function InlineCode({ text }: { text: string }) {
  const parts = text.split(/`([^`]+)`/g);
  return <>{parts.map((p, i) => (i % 2 ? <code key={i}>{p}</code> : p))}</>;
}

export function CoverageCells({ years, statements }: {
  years: number[];
  statements: Partial<Record<StatementPrefix, number[]>>;
}) {
  return (
    <>
      {years.map((y) => {
        const n = (["is", "bs", "cf"] as const).filter((s) => (statements[s] ?? []).includes(y)).length;
        return <i key={y} className={`cov c${n}`} title={`FY${y}: ${n}/3 statements`} />;
      })}
    </>
  );
}

export function Loading({ what = "Loading" }: { what?: string }) {
  return <div className="muted">{what}…</div>;
}

export function ErrorBox({ error }: { error: Error | null }) {
  if (!error) return null;
  return <div className="err-box">{error.message}</div>;
}

/** Common loading/error/empty wrapper for a fetched section. */
export function Section<T>({ state, empty, isEmpty, children }: {
  state: { data: T | null; error: Error | null; loading: boolean };
  empty?: ReactNode;
  isEmpty?: (d: T) => boolean;
  children: (d: T) => ReactNode;
}) {
  if (state.error) return <ErrorBox error={state.error} />;
  if (state.loading && !state.data) return <Loading />;
  if (!state.data) return null;
  if (isEmpty?.(state.data)) return <div className="empty">{empty ?? "Nothing here yet."}</div>;
  return <>{children(state.data)}</>;
}

/** Minimal inline-SVG line chart (no chart library). */
export function LineChart({ points, format, height = 160 }: {
  points: { x: number; y: number }[];
  format: (v: number) => string;
  height?: number;
}) {
  if (points.length < 2) return null;
  const W = 520, H = height, padL = 52, padR = 12, padT = 12, padB = 24;
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys, 0), y1 = Math.max(...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const sx = (x: number) => padL + ((x - x0) / (x1 - x0 || 1)) * (W - padL - padR);
  const sy = (y: number) => padT + (1 - (y - y0) / (y1 - y0)) * (H - padT - padB);
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join("");
  const ticks = [y0, (y0 + y1) / 2, y1];
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img">
      {ticks.map((t) => (
        <g key={t}>
          <line className="axis" x1={padL} x2={W - padR} y1={sy(t)} y2={sy(t)} />
          <text x={padL - 6} y={sy(t) + 4} textAnchor="end">{format(t)}</text>
        </g>
      ))}
      <path className="line" d={d} />
      {points.map((p) => (
        <g key={p.x}>
          <circle className="pt" cx={sx(p.x)} cy={sy(p.y)} r={3}><title>{`${p.x}: ${format(p.y)}`}</title></circle>
          <text x={sx(p.x)} y={H - 6} textAnchor="middle">{p.x}</text>
        </g>
      ))}
    </svg>
  );
}
