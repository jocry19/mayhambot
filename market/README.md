# market/ — breite Marktanalyse

Owned by the thread "Breite Marktanalyse". Misst alle 5 Minuten die Marktlage für Solana-Memecoins und liefert sie
als Zusatzsignal an signals/, sniper/, model/ und das Dashboard. Nur freie Quellen, **keine Helius-Credits**, kein
GeckoTerminal (der knappste Host), nur lesend für alle anderen Pakete.

```bash
.venv/bin/python -m market run              # Dauerbetrieb (Launcher-Dienst "market", Log logs/market.log)
.venv/bin/python -m market once             # eine Runde mit Abruf
.venv/bin/python -m market backfill --hours 24   # Vergangenheit aus der eigenen DB nachrechnen (ohne Netz)
.venv/bin/python -m market status | sectors | terms | coin <MINT>
.venv/bin/python -m market eval [--days 14] [--json]   # taugt die Analyse? (siehe unten)
```

## Interface

```python
import market
market.phase(conn, ts=None, max_age=None) -> dict | None
#  {ts, age_s, score 0-100, label 'risk_off'|'neutral'|'risk_on'|'hot', label_de, coverage 0-1,
#   components {name: {weight, value 0-1, points, why, raw}}, sol_usd, sol_ret_1h/24h/7d, sol_vol_24h,
#   meme_mcap_usd, meme_mcap_chg_24h, breadth, universe_n, volume_h1_usd, volume_ratio, net_flow_usd,
#   launches_1h, launch_ratio, launch_hit2x, launch_rug, launch_migrated, smart_net_usd, smart_buyers, top_sector, top_term}
market.coin_context(conn, mint, ts=None, name=None, symbol=None) -> dict
#  {sector, sector_label, sector_heat, sector_rank, sector_median_ret_h1, rs_sector_h1, terms [{term, heat, lift, hit2x}],
#   term_heat, phase {score, label, label_de, age_s}, age_h, liq_mcap, net_flow_usd, fresh}
market.features(conn, mint, ts=None) -> {name: float | None}   # market.FEATURES, für model/ (additiv)
market.signal_factor(conn, mint, ts=None) -> {factor, why, ...} | None   # None solange MARKET_SIGNAL_WEIGHT aus
market.sniper_adjust(conn, name, symbol, ts=None) -> {delta, why, label}  # delta 0 solange MARKET_SNIPER_GATE aus
market.summary(conn) -> dict | None       # eine Zeile für cli/ und Übersicht
market.view(conn, hours=24) -> dict       # alles für die Dashboard-Seite "Markt" (GET /api/market)
market.evaluation(conn, days=14) -> dict  # Ergebnisse nach Marktphase / Sektor-Hitze / Narrativ
```

Alles ist **point in time**: `ts` liefert nur Snapshots mit Zeitstempel ≤ ts, Launch-Ergebnisse zählen erst, wenn ihr
60-Minuten-Fenster vor ts vorbei war. Damit kann model/ die Features für vergangene Events ohne Blick in die Zukunft
nachschlagen (`market.features(conn, mint, ts=event_ts)`).

## Was gemessen wird

**Marktphase** (0–100, Risk-off < 35 ≤ Neutral < 55 ≤ Risk-on < 75 ≤ Heiß), gewichtete Summe; fehlende Eingänge fallen
heraus, `coverage` sagt wie viel da war:

| Eingang | Gewicht | 0 … 1 |
|---|---|---|
| Smart Money netto 1 h (Ultra/Smart/Good aus `wallets.tier`, `trades`) | 0,16 | tanh(USD / 25k) |
| Breite: Anteil Coins im Plus (1 h) | 0,14 | 25 % … 60 % |
| Launches mit 2x in 60 min (`sniper_launches`, 1–6 h alt) | 0,14 | 3 % … 15 % |
| SOL 24 h (CoinGecko-Historie + Live-Preis) | 0,12 | −8 % … +8 % |
| Solana-Meme-Sektor 24 h (CoinGecko categories) | 0,12 | −15 % … +15 % |
| DEX-Volumen der Top-100 ggü. 24-h-Median | 0,10 | ½x … 2x |
| Launches pro Stunde ggü. 24-h-Median (unter 15 % = Datenlücke, zählt nicht) | 0,08 | ½x … 2x |
| Launches ohne Rug | 0,08 | 85 % … 100 % |
| SOL 7 Tage | 0,06 | −20 % … +20 % |

**Sektoren** (feste Liste in `narratives.SECTORS`: KI, Hunde, Katzen, Frösche, Tiere, Politik, Promis & Elon, Anime,
Gaming, Internet-Kultur, Weltraum, Alltag, Finanzen, Krypto-Meta, Sonstige) aus Name, Symbol und DexScreener-Beschreibung.
Je Sektor: Volumen-Anteil und dessen Änderung ggü. ~1 h (Kapitalrotation), geschätzter Netto-Kauf
(Volumen × (Käufe − Verkäufe) / Trades), Median 1 h, Breite, Smart Money, Launches und deren 2x-Rate → Hitze 0–100.

**Narrative**: Wörter und Wortpaare in Namen der Launches der letzten 3 h gegen 24 h (Anstieg/"Lift"), mit Volumen
der gehandelten Coins und geglätteter 2x-Rate (10 durchschnittliche Pseudo-Launches). Beispiel aus den echten Daten
vom 16.10.2026: "flying", "tesla", "roadster" in 7 % aller Launches.

**Wo das Geld sitzt**: Volumen-Anteil, Netto-Kauf, Median 1 h und Smart Money nach Market Cap (< 100k, 100k–1M,
1–10M, > 10M) und Alter (< 1 h, 1–6 h, 6–24 h, 1–7 d, > 7 d).

## Dynamik (`market/dynamics.py`, stündlich)

- **Regime mit Übergängen**: das Label bekommt ±3 Punkte Hysterese (`regime`, `regime_since`), dazu EMA und Steigung
  über 1 h und 3 h (kleinste Quadrate) und ein Trend: stabil, steigt, fällt, kippt nach oben, kippt nach unten.
  `transitions` zählt stündliche Wechsel der letzten 14 Tage und gibt Laplace-geglättete Wahrscheinlichkeiten
  "von Phase A in der nächsten Stunde nach B".
- **Narrative mit Lebenszyklus**: jede Narrativ-Zeile bekommt `stage` (neu, wachsend, Höhepunkt, reif, abklingend),
  `first_seen`, `peak_n`, `age_h` und `growth_1h` aus ihrer eigenen Historie.
- **Sektor-Rotation mit Vorlauf**: `sector_lead` misst je Stunde quer über die Sektoren die Spearman-Rangkorrelation
  zwischen Volumen-Anteils-Zuwachs, Smart-Money-Zufluss oder Hitze und der Sektor-Rendite der *folgenden* Stunde.
  Bestätigt erst bei ≥ 12 Stunden, |t| ≥ 2 und gleichem Vorzeichen in beiden Hälften. `rotation_streaks` zeigt, wer
  seit mehreren Runden Anteil gewinnt.
- **Messung je Baustein**: `component_power` korreliert jede Phasen-Komponente mit der 2x-Rate der Launches der
  folgenden Stunde (Rang-IC, ältere und neuere Hälfte getrennt). `learn_weights` passt Gewichte nur auf der älteren
  Hälfte an, zieht sie zu 50 % zum Ausgangswert zurück und übernimmt sie nur, wenn sie auf der neueren Hälfte um
  ≥ 0,05 IC besser sind. Wirkt erst mit `MARKET_LEARNED_WEIGHTS=true` (Standard aus).
- Alles Zeitpunkt-genau: nur Zeilen mit ts ≤ t, Launch-Ergebnisse erst nach Ablauf ihres 60-min-Fensters, und eine
  Coin-Zeile, die nach t geschrieben wurde, zählt für t nicht als frisch.
- Ergebnis in `market_state` (`dynamics`, `learned_weights`), Intervall `MARKET_DYNAMICS_EVERY_SEC` (3600).
- Mit jeder Dynamik-Runde schreibt der Dienst `reports/market_report.json` neben der DB (Auswertung mit Walk-forward,
  Bausteine, Sektor-Vorlauf, Schalter und `walk_forward_says`) und eine Log-Zeile "Taugt die Marktanalyse? ...".
  Damit lässt sich die Messung ohne Zugriff auf die DB lesen.

## Wirkung auf andere Pakete (aus, bis gemessen)

- `signals/engine.apply_market`: Score × `signal_factor` (60 % Phase, 40 % Sektor-Hitze, höchstens ±15 %), eigene
  Zeile `market` in `contributions_json`. Schalter `MARKET_SIGNAL_WEIGHT` (Standard aus).
- `sniper/engine.market_gate`: in Risk-off Mindest-Score +10, bei heißem Narrativ mit besserer 2x-Rate −5; ändert nur
  "Score zu niedrig" oder einen sauberen Kauf, nie Bundle/Insider/Risiko. Schalter `MARKET_SNIPER_GATE` (Standard aus),
  Skip-Grund `market`.
- `MARKET_SNIPER_NEW_TERM` (Standard aus): Mindest-Score +`MARKET_SNIPER_NEW_TERM_BUMP` (5), wenn das stärkste Narrativ
  eines Launches brandneu ist (Stufe "neu"). Messung am 16.10.2026, 15 873 Launches: neu 6 % 2x gegen 10 % im Rest.
- Ein Schalter gilt in `walk_forward_says` erst als getragen, wenn die ältere Hälfte selbst klar war (z ≥ 2), die
  neuere sie bestätigt (z ≥ 2) und die Richtung genau die Regel des Schalters ist.
- Ein Snapshot älter als `MARKET_MAX_AGE_SEC` (30 min) wirkt nie.

**Einschalten erst**, wenn `python -m market eval` (oder die Karte "Taugt die Marktanalyse?") einen klaren Unterschied
auch im Walk-forward (ältere Hälfte wählt, neuere bestätigt) zeigt: Trefferquote der besten gegen die schlechteste Gruppe, Zwei-Stichproben-z-Test |z| ≥ 2, je Gruppe ≥ 40 Launches.
Die Karte zeigt auch, was das Sniper-Tor im Rückblick gesperrt hätte.

## Kosten und Last

- DexScreener: etwa 10 Batch-Calls je Runde (300 Coins à 30) + 3 Calls für Boosts/Profile alle 10 min ≈ 2–3 Calls/min,
  eigenes Budget `MARKET_DEXSCREENER_PER_MIN` (20).
- CoinGecko public: 2 Calls alle 15 min (SOL-Historie stündlich, Meme-Sektor), Budget 4/min.
- SOL-Live-Preis über `core.sources.market.sol_price_usd` (dort gecacht). Helius und GeckoTerminal: 0.
- DB: je Runde 1 Snapshot + ~15 Sektor- + ≤ 40 Narrativ- + 9 Bucket-Zeilen; `market_tokens` ≤ ~500 Zeilen.
  Aufbewahrung `MARKET_KEEP_DAYS` (60). Eine Runde rechnet in wenigen Sekunden.

## Tabellen

| Tabelle | Inhalt |
|---|---|
| `market_snapshots` | ts PK, score, label, regime, regime_since, score_ema, slope_1h, slope_3h, trend, coverage, components_json, SOL-Renditen/Vola, Meme-Sektor, breadth, universe_n, volume_h1_usd, volume_ratio, net_flow_usd, launches_1h, launch_ratio, launch_hit2x, launch_rug, launch_migrated, smart_net_usd, smart_buyers, top_sector, top_term, backfilled |
| `market_sectors` | (ts, sector) PK, n_tokens, mcap_usd, volume_h1_usd, vol_share, vol_share_chg, net_flow_usd, median_ret_h1, breadth, smart_net_usd, smart_buyers, launches, launch_hit2x, heat, top_json |
| `market_terms` | (ts, term) PK, n_recent, n_base, lift, volume_h1_usd, hit2x, rug, n_outcomes, heat, mints_json, stage, first_seen, peak_n, age_h, growth_1h |
| `market_buckets` | (ts, kind 'mcap'/'age', bucket) PK, n, volume_h1_usd, vol_share, net_flow_usd, median_ret_h1, smart_net_usd |
| `market_tokens` | mint PK, Metadaten (Beschreibung, Socials, Boosts), sectors_json, terms_json, Marktwerte mit Käufen/Verkäufen 1 h |
| `market_sol` | ts PK, price_usd (SOL-Verlauf) |
| `market_state` | key PK, value (`service_status` = Heartbeat je Runde, `dynamics`, `learned_weights`) |
