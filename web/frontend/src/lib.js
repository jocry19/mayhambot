import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

// ------------------------------------------------------------------ API
export async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  let data = null;
  try { data = await r.json(); } catch { /* empty */ }
  if (!r.ok) throw new Error((data && (data.detail || data.message)) || `HTTP ${r.status}`);
  return data;
}

const refreshBus = new Set();
export function refreshAll() { refreshBus.forEach((f) => f()); }

/** Poll a JSON endpoint; keeps the previous data while refetching (no skeleton flash). */
export function usePoll(path, interval = 10000) {
  const [state, set] = useState({ data: null, error: null, loading: true, at: 0 });
  const live = useRef(true);
  const cur = useRef(path);
  cur.current = path;
  const load = useCallback(async () => {
    if (!path) return;
    set((s) => ({ ...s, loading: true }));
    try {
      const d = await api(path);
      if (live.current && cur.current === path) set({ data: d, error: null, loading: false, at: Date.now() });
    } catch (e) {
      if (live.current && cur.current === path) set((s) => ({ ...s, error: e.message, loading: false }));
    }
  }, [path]);
  useEffect(() => {
    live.current = true;
    set((s) => ({ ...s, data: s.data && cur.current === path ? s.data : null, loading: true }));
    load();
    const paused = () => document.hidden;
    const t = interval ? setInterval(() => !paused() && load(), interval) : null;
    refreshBus.add(load);
    return () => { live.current = false; t && clearInterval(t); refreshBus.delete(load); };
  }, [path, interval, load]);
  return { ...state, reload: load };
}

// ------------------------------------------------------------------ router (hash)
function getHash() { return window.location.hash.replace(/^#/, "") || "/"; }
function subHash(cb) { window.addEventListener("hashchange", cb); return () => window.removeEventListener("hashchange", cb); }
export function useRoute() {
  const h = useSyncExternalStore(subHash, getHash);
  const [path, query = ""] = h.split("?");
  const parts = path.split("/").filter(Boolean);
  return { path, parts, query: Object.fromEntries(new URLSearchParams(query)) };
}
export function go(to) { window.location.hash = to; }

// ------------------------------------------------------------------ toasts
let toasts = [];
const toastSubs = new Set();
export function toast(msg, err = false) {
  const t = { id: Math.random(), msg, err };
  toasts = [...toasts, t];
  toastSubs.forEach((f) => f());
  setTimeout(() => { toasts = toasts.filter((x) => x !== t); toastSubs.forEach((f) => f()); }, err ? 7000 : 4000);
}
export function useToasts() {
  return useSyncExternalStore((cb) => { toastSubs.add(cb); return () => toastSubs.delete(cb); }, () => toasts);
}
export async function act(path, body, okMsg) {
  try {
    const r = await api(path, { method: "POST", body: body || {} });
    toast(okMsg || r.message || "OK");
    refreshAll();
    return r;
  } catch (e) {
    toast(e.message, true);
    throw e;
  }
}

// ------------------------------------------------------------------ theme
export function getTheme() {
  try { return localStorage.getItem("mh-theme") || "dark"; } catch { return "dark"; }
}
export function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("mh-theme", t); } catch { /* ignore */ }
  window.dispatchEvent(new Event("mh-theme"));
}
export function useThemeName() {
  return useSyncExternalStore((cb) => { window.addEventListener("mh-theme", cb); return () => window.removeEventListener("mh-theme", cb); },
    () => document.documentElement.dataset.theme || "dark");
}
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ------------------------------------------------------------------ format (German)
const nf = (d) => new Intl.NumberFormat("de-DE", { maximumFractionDigits: d, minimumFractionDigits: d });
const NF = [0, 1, 2, 3, 4].map(nf);
export const n = (v, d = 0) => (v == null || Number.isNaN(v) ? "–" : NF[d].format(v));
export function compact(v, d = 1) {
  if (v == null || Number.isNaN(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e9) return NF[d].format(v / 1e9) + " Mrd";
  if (a >= 1e6) return NF[d].format(v / 1e6) + " Mio";
  if (a >= 1e3) return NF[d].format(v / 1e3) + "k";
  return NF[a < 10 ? Math.min(d + 1, 2) : 0].format(v);
}
export const usd = (v, d = 1) => (v == null ? "–" : (v < 0 ? "−$" : "$") + compact(Math.abs(v), d));
export const usdSigned = (v, d = 1) => (v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + "$" + compact(Math.abs(v), d));
export const mc = (v) => (v == null ? "–" : "$" + compact(v, v >= 1e6 ? 2 : 1));
export const sol = (v, d = 2) => (v == null ? "–" : `${n(v, d)} SOL`);
export const solSigned = (v, d = 3) => (v == null ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${n(Math.abs(v), d)} SOL`);
export const pct = (v, d = 0) => (v == null ? "–" : `${n(v * 100, d)} %`);
export const pctSigned = (v, d = 1) => (v == null ? "–" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${n(Math.abs(v * 100), d)} %`);
export function price(v) {
  if (v == null) return "–";
  if (v >= 1) return "$" + n(v, 2);
  const s = v.toPrecision(3);
  return "$" + (v < 1e-4 ? Number(s).toExponential(2).replace(".", ",") : s.replace(".", ","));
}
export function ago(ts) {
  if (!ts) return "–";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `vor ${Math.floor(s)} s`;
  if (s < 3600) return `vor ${Math.floor(s / 60)} min`;
  if (s < 86400) return `vor ${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `vor ${Math.floor(s / 86400)} T ${Math.floor((s % 86400) / 3600)} h`;
}
export function dt(ts, withDate = true) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  if (!withDate) return t;
  const today = new Date().toDateString() === d.toDateString();
  return today ? t : `${d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })} ${t}`;
}
export function dur(min) {
  if (min == null) return "–";
  if (min < 1) return `${Math.round(min * 60)} s`;
  if (min < 90) return `${n(min, 0)} min`;
  return `${n(min / 60, 1)} h`;
}
export const short = (a, k = 4) => (a ? `${a.slice(0, k)}…${a.slice(-k)}` : "–");
export const sign = (v) => (v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");
export const TIER_LABEL = { ultra: "Ultra Smart", smart: "Smart", good: "Good", other: "Andere" };
export const TIERS = ["ultra", "smart", "good"];
export const STRAT_LABEL = { whale: "Modus 1 · Wal-Pumps", signal: "Modus 2 · Signale", manual: "Manuell" };
export const MODE_LABEL = { off: "Aus", manual: "Manuell", semi: "Halbautomatisch", auto: "Automatisch" };
export const MODE_SHORT = { off: "Aus", manual: "Manuell", semi: "Halbauto", auto: "Auto" };
export const EXIT_LABEL = {
  target: "Ziel erreicht", inflow_peak: "Copy-Ansturm vorbei", trail: "Trailing-Stop", stop: "Stop-Loss",
  time: "Zeit abgelaufen", whale: "Wal verkauft", no_market: "kein Markt", smart_exit: "Smart Money steigt aus", manual: "von dir verkauft",
};
export const TIER_SOURCE = {
  model: "Tiers aus dem Forschungsmodell",
  core: "Tiers aus der Signal-Engine (vorläufig)",
  fallback: "Tiers vorläufig aus dem Wallet-Score, bis das Modell gelaufen ist",
};
