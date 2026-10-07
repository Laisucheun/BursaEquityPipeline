import { useCallback, useEffect, useRef, useState } from "react";

export const fmt = (n: number | null | undefined) => Number(n || 0).toLocaleString();

export const pct = (a: number, b: number) => (b ? (100 * a) / b : 0);

/** Ringgit amount, compact (RM 1.23b / 456.7m / 12.3k). */
export function money(v: number | null | undefined): string {
  if (v == null) return "–";
  const a = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  if (a >= 1e9) return `${sign}${(a / 1e9).toFixed(2)}b`;
  if (a >= 1e6) return `${sign}${(a / 1e6).toFixed(1)}m`;
  if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(1)}k`;
  return `${sign}${a.toLocaleString(undefined, { maximumFractionDigits: 4 })}`;
}

/** Full-precision number for tables of raw facts. */
export const num = (v: number | null | undefined) =>
  v == null ? "–" : v.toLocaleString(undefined, { maximumFractionDigits: 4 });

export const ratio = (v: number | null | undefined, digits = 2) => (v == null ? "–" : v.toFixed(digits));

export const percent = (v: number | null | undefined, digits = 1) =>
  v == null ? "–" : `${(v * 100).toFixed(digits)}%`;

export function dur(s: number | null | undefined): string {
  if (s == null) return "–";
  s = Math.round(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "–";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export interface AsyncState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  reload: () => void;
}

/** Run `fn` when `deps` change (or when `enabled` flips on). Ignores stale responses. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[], enabled = true): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [tick, setTick] = useState(0);
  const seq = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    const id = ++seq.current;
    setLoading(true);
    setError(null);
    fn().then(
      (d) => {
        if (id === seq.current) {
          setData(d);
          setLoading(false);
        }
      },
      (e: unknown) => {
        if (id === seq.current) {
          setError(e instanceof Error ? e : new Error(String(e)));
          setLoading(false);
        }
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, error, loading, reload };
}

export function useDebounced<T>(value: T, ms = 250): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}
