import { useMemo, useState } from "react";
import { TIER_LABEL, pctSigned, sign, short, toast, useToasts } from "./lib.js";

// ------------------------------------------------------------------ icons (inline, stroke)
const P = {
  home: "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  bolt: "M13 2 4 14h7l-1 8 9-12h-7z",
  coins: "M8 8a6 3 0 1 0 12 0 6 3 0 1 0-12 0M8 8v4c0 1.7 2.7 3 6 3s6-1.3 6-3V8M4 12a6 3 0 0 0 6 3M4 12v4c0 1.7 2.7 3 6 3 1 0 2-.1 2.8-.4M4 12c0-1.2 1.4-2.3 3.5-2.7",
  wallet: "M3 7a2 2 0 0 1 2-2h13v4M3 7v11a2 2 0 0 0 2 2h15V9H5a2 2 0 0 1-2-2zM16 14.5h.01",
  copy: "M4 17 9 12l4 4 7-8M15 8h5v5",
  brain: "M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 3 3V4zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-3 3V4z",
  sliders: "M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0M16 4v4M10 10v4M18 16v4",
  server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-4-4",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  refresh: "M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",
  ext: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  clip: "M9 9h10v12H9zM5 15H4V3h12v1",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
  alert: "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01",
  pause: "M8 5v14M16 5v14",
  play: "M6 4l14 8-14 8z",
  chevron: "M9 6l6 6-6 6",
  x: "M6 6l12 12M18 6 6 18",
  star: "M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z",
  terminal: "M4 17l6-6-6-6M12 19h8",
};
export function Icon({ name, size }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
      style={size ? { width: size, height: size } : undefined} aria-hidden="true">
      <path d={P[name] || P.info} />
    </svg>
  );
}

// ------------------------------------------------------------------ layout atoms
export function Card({ title, sub, actions, children, className = "", flush, style }) {
  return (
    <section className={`card ${className}`} style={style}>
      {(title || actions) && (
        <div className="card-h">
          <div>
            {title && <div className="card-t">{title}</div>}
            {sub && <div className="card-s">{sub}</div>}
          </div>
          {actions && <div className="card-a">{actions}</div>}
        </div>
      )}
      <div className={`card-b ${flush ? "flush" : ""}`}>{children}</div>
    </section>
  );
}

export function Tile({ label, value, sub, cls, title }) {
  return (
    <div className="tile" title={title}>
      <div className="tile-l">{label}</div>
      <div className={`tile-v ${cls || ""}`}>{value}</div>
      {sub != null && <div className="tile-s">{sub}</div>}
    </div>
  );
}

export function Empty({ children }) {
  return <div className="empty">{children || "Noch keine Daten"}</div>;
}

export function Callout({ kind = "", icon = "info", children }) {
  return <div className={`callout ${kind}`}><Icon name={icon} /><div>{children}</div></div>;
}

export function Seg({ value, onChange, options }) {
  return (
    <div className="seg" role="tablist">
      {options.map((o) => {
        const [v, l] = Array.isArray(o) ? o : [o, o];
        return <button key={String(v)} className={value === v ? "on" : ""} onClick={() => onChange(v)} role="tab" aria-selected={value === v}>{l}</button>;
      })}
    </div>
  );
}

export function Switch({ on, onChange, disabled, red, label }) {
  return (
    <button type="button" className={`switch ${on ? "on" : ""} ${red ? "red" : ""} ${disabled ? "disabled" : ""}`}
      aria-pressed={on} aria-label={label} disabled={disabled} onClick={() => !disabled && onChange(!on)} />
  );
}

// ------------------------------------------------------------------ domain atoms
export function Tier({ t }) {
  if (!t) return <span className="dim">–</span>;
  return <span className={`tier tier-${t}`}><i />{TIER_LABEL[t] || t}</span>;
}

export function TierCounts({ tiers, title }) {
  const t = tiers || {};
  return (
    <span className="tc" title={title || `${t.ultra || 0} Ultra Smart · ${t.smart || 0} Smart · ${t.good || 0} Good`}>
      {["ultra", "smart", "good"].map((k) => (
        <span key={k} className={t[k] ? "on" : ""}><i style={{ background: `var(--tier-${k})` }} />{t[k] || 0}</span>
      ))}
    </span>
  );
}

export function Score({ v, g }) {
  if (v == null) return <span className="dim">–</span>;
  const grade = g || (v >= 80 ? "A" : v >= 65 ? "B" : v >= 50 ? "C" : v >= 35 ? "D" : "E");
  return (
    <span className="score" title={`Score ${Math.round(v)} / 100, Note ${grade}`}>
      <span className={`score-g g${grade}`}>{grade}</span>
      <span className="score-bar"><b style={{ width: `${Math.max(2, Math.min(100, v))}%` }} /></span>
      <span className="score-n">{Math.round(v)}</span>
    </span>
  );
}

export function Delta({ v, d = 1, arrow = true }) {
  if (v == null) return <span className="dim">–</span>;
  return <span className={sign(v)}>{arrow && v !== 0 ? (v > 0 ? "▲ " : "▼ ") : ""}{pctSigned(v, d)}</span>;
}

export function Addr({ a, k = 4, link }) {
  if (!a) return <span className="dim">–</span>;
  const copy = (e) => {
    e.stopPropagation();
    navigator.clipboard?.writeText(a).then(() => toast("Adresse kopiert"));
  };
  return (
    <span className="mono" title={a}>
      {link ? <a href={link} onClick={(e) => e.stopPropagation()}>{short(a, k)}</a> : short(a, k)}
      <button className="btn ghost sm" style={{ height: 20, padding: "0 4px", marginLeft: 2 }} onClick={copy} aria-label="Adresse kopieren"><Icon name="clip" size={12} /></button>
    </span>
  );
}

export function Bars({ items, fmt = (v) => v, max }) {
  const m = max ?? Math.max(1e-9, ...items.map((i) => Math.abs(i.value || 0)));
  const hasNeg = items.some((i) => (i.value || 0) < 0);
  return (
    <div className="bars">
      {items.map((i, k) => {
        const w = (Math.abs(i.value || 0) / m) * (hasNeg ? 50 : 100);
        const left = hasNeg ? ((i.value || 0) < 0 ? 50 - w : 50) : 0;
        return (
          <div className="bar-row" key={k} title={i.title}>
            <div className="bl">{i.label}</div>
            <div className="bt">
              {hasNeg && <span style={{ position: "absolute", left: "50%", top: -2, bottom: -2, width: 1, background: "var(--axis)" }} />}
              <b className={(i.value || 0) < 0 ? "neg" : ""} style={{ left: `${left}%`, width: `${Math.max(w, 0.5)}%`, background: i.color }} />
            </div>
            <div className="bv">{fmt(i.value, i)}</div>
          </div>
        );
      })}
    </div>
  );
}

export function Legend({ items }) {
  return (
    <div className="legend">
      {items.map((i) => <span key={i.label}><i style={{ background: i.color }} />{i.label}</span>)}
    </div>
  );
}

// ------------------------------------------------------------------ table
/** cols: [{k, l, r (right), fmt(row), sort (value fn or key), w, title}] */
export function Table({ rows, cols, onRow, rowKey = (r, i) => i, initialSort, empty, tall, expand, selected, max = 500 }) {
  const [s, setS] = useState(initialSort || null);
  const [open, setOpen] = useState(null);
  const sorted = useMemo(() => {
    if (!rows) return [];
    if (!s) return rows;
    const c = cols.find((c) => c.k === s.k);
    if (!c) return rows;
    const f = typeof c.sort === "function" ? c.sort : (r) => r[c.sort || c.k];
    return [...rows].sort((a, b) => {
      const x = f(a), y = f(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (x < y ? -1 : x > y ? 1 : 0) * (s.d === "asc" ? 1 : -1);
    });
  }, [rows, s, cols]);
  if (!rows || !rows.length) return <Empty>{empty}</Empty>;
  return (
    <div className={`tbl-wrap ${tall ? "tall" : ""}`}>
      <table className="tbl">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.k} className={`${c.r ? "r" : ""} ${c.sort !== false ? "sortable" : ""}`} style={{ width: c.w }} title={c.title}
                onClick={() => c.sort !== false && setS((p) => ({ k: c.k, d: p && p.k === c.k && p.d === "desc" ? "asc" : "desc" }))}>
                {c.l}{s && s.k === c.k ? (s.d === "desc" ? " ↓" : " ↑") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.slice(0, max).map((r, i) => {
            const key = rowKey(r, i);
            const isOpen = expand && open === key;
            return [
              <tr key={key} className={`${onRow || expand ? "click" : ""} ${selected === key ? "sel" : ""} ${isOpen ? "expanded" : ""}`}
                onClick={() => (expand ? setOpen(isOpen ? null : key) : onRow && onRow(r))}>
                {cols.map((c) => <td key={c.k} className={c.r ? "r" : ""}>{c.fmt ? c.fmt(r) : r[c.k] ?? "–"}</td>)}
              </tr>,
              isOpen && <tr key={key + "-x"}><td className="detail" colSpan={cols.length}>{expand(r)}</td></tr>,
            ];
          })}
        </tbody>
      </table>
      {sorted.length > max && <div className="empty">{sorted.length - max} weitere Zeilen ausgeblendet</div>}
    </div>
  );
}

// ------------------------------------------------------------------ modal + toasts
export function Modal({ title, children, onClose, actions }) {
  return (
    <div className="modal-bg" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {children}
        <div className="row" style={{ justifyContent: "flex-end" }}>{actions}</div>
      </div>
    </div>
  );
}

export function Toasts() {
  const ts = useToasts();
  return <div className="toasts" aria-live="polite">{ts.map((t) => <div key={t.id} className={`toast ${t.err ? "err" : ""}`}>{t.msg}</div>)}</div>;
}

// ------------------------------------------------------------------ tiny markdown (reports)
export function Markdown({ text }) {
  const html = useMemo(() => {
    const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
    const out = [];
    let list = false, table = [];
    const flushT = () => {
      if (!table.length) return;
      const rows = table.filter((r) => !/^\|\s*-/.test(r)).map((r) => r.replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim())));
      out.push("<table>" + rows.map((r, i) => "<tr>" + r.map((c) => (i ? `<td>${c}</td>` : `<th>${c}</th>`)).join("") + "</tr>").join("") + "</table>");
      table = [];
    };
    for (const line of (text || "").split("\n")) {
      if (line.startsWith("|")) { table.push(line); continue; }
      flushT();
      if (/^\s*[-*] /.test(line)) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(line.replace(/^\s*[-*] /, ""))}</li>`); continue; }
      if (list) { out.push("</ul>"); list = false; }
      if (line.startsWith("### ")) out.push(`<h3>${inline(line.slice(4))}</h3>`);
      else if (line.startsWith("## ")) out.push(`<h2>${inline(line.slice(3))}</h2>`);
      else if (line.startsWith("# ")) out.push(`<h2>${inline(line.slice(2))}</h2>`);
      else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
    }
    flushT();
    if (list) out.push("</ul>");
    return out.join("");
  }, [text]);
  return <div className="md" dangerouslySetInnerHTML={{ __html: html }} />;
}

export function Tabs({ value, onChange, options }) {
  return (
    <div className="tabs" role="tablist">
      {options.map(([v, l]) => <button key={v} className={value === v ? "on" : ""} onClick={() => onChange(v)} role="tab" aria-selected={value === v}>{l}</button>)}
    </div>
  );
}
