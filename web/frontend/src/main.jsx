import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { MODE_LABEL, api, ago, go, getTheme, mc, refreshAll, setTheme, usePoll, useRoute, useThemeName } from "./lib.js";
import { Icon, Toasts } from "./ui.jsx";
import Overview from "./pages/Overview.jsx";
import Signals from "./pages/Signals.jsx";
import Coins from "./pages/Coins.jsx";
import Coin from "./pages/Coin.jsx";
import Wallets from "./pages/Wallets.jsx";
import Wallet from "./pages/Wallet.jsx";
import CopyTrade from "./pages/CopyTrade.jsx";
import Model from "./pages/Model.jsx";
import Control from "./pages/Control.jsx";
import System from "./pages/System.jsx";
import Sniper from "./pages/Sniper.jsx";
import Market from "./pages/Market.jsx";

setTheme(getTheme());

const NAV = [
  ["/", "home", "Übersicht"],
  ["/signals", "bolt", "Signale"],
  ["/coins", "coins", "Coins"],
  ["/wallets", "wallet", "Wallets"],
  ["/copytrade", "copy", "Copy-Trading"],
  ["/sniper", "target", "Launch-Sniper"],
  ["/market", "globe", "Markt"],
  ["/model", "brain", "Modell & Forschung"],
  ["sep"],
  ["/control", "sliders", "Steuerung"],
  ["/system", "server", "System"],
];

function Search() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState([]);
  const [sel, setSel] = useState(0);
  const t = useRef();
  useEffect(() => {
    clearTimeout(t.current);
    if (q.trim().length < 2) { setRes([]); return; }
    t.current = setTimeout(() => api(`/api/search?search=${encodeURIComponent(q.trim())}`).then(setRes).catch(() => setRes([])), 180);
  }, [q]);
  const open = (r) => { go(r.type === "coin" ? `/coin/${r.id}` : `/wallet/${r.id}`); setQ(""); setRes([]); };
  const onKey = (e) => {
    if (e.key === "ArrowDown") { setSel((s) => Math.min(res.length - 1, s + 1)); e.preventDefault(); }
    if (e.key === "ArrowUp") { setSel((s) => Math.max(0, s - 1)); e.preventDefault(); }
    if (e.key === "Enter") {
      if (res[sel]) open(res[sel]);
      else if (q.trim().length >= 32) go(q.trim().endsWith("pump") || q.trim().length < 43 ? `/coin/${q.trim()}` : `/wallet/${q.trim()}`);
    }
    if (e.key === "Escape") { setQ(""); setRes([]); }
  };
  return (
    <div className="search">
      <Icon name="search" />
      <input value={q} onChange={(e) => { setQ(e.target.value); setSel(0); }} onKeyDown={onKey}
        placeholder="Coin, Mint oder Wallet suchen …" aria-label="Suchen" />
      {res.length > 0 && (
        <div className="search-res">
          {res.map((r, i) => (
            <a key={r.type + r.id} className={i === sel ? "sel" : ""} href={r.type === "coin" ? `#/coin/${r.id}` : `#/wallet/${r.id}`} onClick={() => { setQ(""); setRes([]); }}>
              <span><b>{r.label}</b> <span className="dim">{r.sub}</span></span>
              <span className="dim">{r.type === "coin" ? `Coin · MC ${mc(r.mcap_usd)}` : "Wallet"}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function SideFoot() {
  const { data } = usePoll("/api/control", 15000);
  if (!data || !data.available) return <div className="side-foot" />;
  return (
    <div className="side-foot">
      <div className="mode-line"><span>Konto</span>{data.live_ready && (data.live?.whale || data.live?.signal)
        ? <span className="badge live">ECHTGELD</span> : <span className="badge good">Papier</span>}</div>
      <div className="mode-line"><span>Modus 1 Wale</span><span className="t2">{MODE_LABEL[data.modes?.whale] || "–"}</span></div>
      <div className="mode-line"><span>Modus 2 Signale</span><span className="t2">{MODE_LABEL[data.modes?.signal] || "–"}</span></div>
      {data.paused && <span className="badge warn">Käufe pausiert</span>}
    </div>
  );
}

function Page({ route }) {
  const [a, b] = route.parts;
  switch (a) {
    case undefined: return <Overview />;
    case "signals": return <Signals />;
    case "coins": return <Coins />;
    case "coin": return <Coin mint={b} hl={route.query.signal} />;
    case "wallets": return <Wallets />;
    case "wallet": return <Wallet address={b} />;
    case "copytrade": return <CopyTrade />;
    case "sniper": return <Sniper />;
    case "market": return <Market />;
    case "model": return <Model />;
    case "control": return <Control />;
    case "system": return <System />;
    default: return <Overview />;
  }
}

const TITLES = { "": "Übersicht", signals: "Signale", coins: "Coins", coin: "Coin", wallets: "Wallets", wallet: "Wallet",
  copytrade: "Copy-Trading", sniper: "Launch-Sniper", market: "Markt", model: "Modell & Forschung", control: "Steuerung", system: "System" };

function Heartbeat() {
  const { data, error } = usePoll("/api/system", 20000);
  const last = data?.last?.trades;
  const stale = !last || Date.now() / 1000 - last > 900;
  return (
    <span className="row nowrap dim" style={{ fontSize: 12 }} title={error ? `API-Fehler: ${error}` : "Letzter erfasster Trade"}>
      <span className={`pulse ${stale || error ? "stale" : ""}`} />
      {error ? "API nicht erreichbar" : last ? `Daten ${ago(last)}` : "keine Daten"}
    </span>
  );
}

function App() {
  const route = useRoute();
  const theme = useThemeName();
  const first = "/" + (route.parts[0] || "");
  useEffect(() => { window.scrollTo(0, 0); }, [route.path]);
  useEffect(() => { document.title = `${TITLES[route.parts[0] || ""] || "mayhambot"} · mayhambot`; }, [route.parts]);
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <div className="brand-mark">M</div>
          <div><div className="brand-name">mayhambot</div><div className="brand-sub">Smart Money · Copy-Trading</div></div>
        </div>
        <nav className="nav">
          {NAV.map(([to, icon, label], i) => to === "sep" ? <div key={i} className="nav-sep" /> : (
            <a key={to} href={`#${to}`} className={(to === "/" ? route.parts.length === 0 : first === to || (to === "/coins" && first === "/coin") || (to === "/wallets" && first === "/wallet")) ? "on" : ""}>
              <Icon name={icon} /><span>{label}</span>
            </a>
          ))}
        </nav>
        <SideFoot />
      </aside>
      <main className="main">
        <header className="top">
          <h1>{TITLES[route.parts[0] || ""] || "mayhambot"}</h1>
          <Search />
          <div className="top-right">
            <Heartbeat />
            <button className="btn ghost sm" onClick={refreshAll} title="Neu laden" aria-label="Neu laden"><Icon name="refresh" /></button>
            <button className="btn ghost sm" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} title="Hell/Dunkel" aria-label="Farbschema wechseln">
              <Icon name={theme === "dark" ? "sun" : "moon"} />
            </button>
          </div>
        </header>
        <div className="content"><Page route={route} /></div>
      </main>
      <Toasts />
    </div>
  );
}

createRoot(document.getElementById("root")).render(<StrictMode><App /></StrictMode>);
