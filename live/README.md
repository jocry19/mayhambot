# live/ · Echtgeld-Pfad

Alles, was zwischen einer Kaufentscheidung der Strategie (copytrade/) und einer echten Transaktion steht:
Ausführung, harte Grenzen, Not-Aus, Go-Live-Prüfung. Ohne `LIVE_TRADING=true` passiert hier nichts.

## Ablauf eines Kaufs (`executor.py`)

1. **Guard** (`guard.py`, unabhängig von Strategie und Preset): Not-Aus, unklare Order offen, Fehlerserie
   (3 in Folge setzt Not-Aus), Datenausfall, Live-Wächter läuft (nur Echtgeld), Go-Live-Prüfung bestanden
   (nur Echtgeld), max. offene Positionen, Einsatz-Obergrenze, Verlust 24 h (realisiert plus offene Verluste),
   Trading-Wallet nicht zu groß (Trennung vom Hauptvermögen), Reserve für Gebühren. Größe wird auf die Stufe gekappt.
2. Jupiter-Quote mit **unserer** Slippage (kein dynamicSlippage), Preiseinfluss ≤ 3 %, Quote-Preis höchstens 25 %
   neben dem DexScreener-Preis, Marktdaten nicht älter als 90 s.
3. Swap-Transaktion mit Priority-Fee-Deckel (0,0003 SOL), lokal signiert.
4. Senden mit Preflight (die RPC simuliert vorher: ein scheiternder Swap kostet nichts), Rebroadcast alle 3 s bis
   bestätigt oder die Blockhöhe über `lastValidBlockHeight` ist (dann sicher nicht gelandet). Ohne klares Ergebnis
   bleibt die Order `unknown` und blockiert neue Käufe, bis der Wächter sie geklärt hat.
5. Gebucht werden die **gelandeten** Mengen aus der Transaktion (Token roh, SOL inkl. Gebühr und Kontomiete),
   in `ct_positions` wie beim Papierhandel (mode `live`). Realisierte Slippage gegen das Quote steht in `live_orders`.

Verkauf: der ganze On-Chain-Bestand des Coins (nicht die gebuchte Float-Menge), bis zu 3 Versuche mit frischem
Quote und doppelter Slippage (500 → 1000 → 2000 bps, max. 2500), danach Pausen 30 s bis 10 min, ab 6 Fehlrunden
Telegram-Alarm. Ein früherer, spät gelandeter Verkauf wird gebucht statt doppelt verkauft. Danach wird das leere
Token-Konto geschlossen (~0,002 SOL Miete zurück, bei 0,1 SOL Einsatz sonst 2 % Verlust je Trade).

## Dry-Run (Standard, solange `LIVE_DRY_RUN` nicht `false` ist)

Gleicher Weg bis zur Signatur, Kauf wird von der RPC simuliert, nichts wird gesendet. Positionen laufen als
mode `dry` durch den echten Handelsablauf (Kauf, Ausstiege, Verkauf) mit Quote-basiertem Fill wie im Papiermodus,
eigenes virtuelles Guthaben (`LIVE_DRY_START_SOL`, 1 SOL). Damit die Simulation klappt, braucht die Trading-Wallet
etwas SOL (mindestens die Positionsgröße). Einzeltest: `python -m live dryrun <MINT> [SOL]`.

## Stufenplan (`config.py`)

| Stufe | pro Trade | offen | Verlust 24 h | Einsatz gesamt | weiter, wenn |
|---|---|---|---|---|---|
| 1 | 0,1 SOL | 2 | 0,3 SOL | 0,2 SOL | 20 Live-Runden, netto > 0, Slippage-Parität, ≥ 90 % gelandet |
| 2 | 0,25 SOL | 3 | 0,75 SOL | 0,75 SOL | 40 Live-Runden, gleiche Kriterien |
| 3 | 0,5 SOL | 5 | 1,5 SOL | 2,5 SOL | |

`.env`-Werte (`LIVE_MAX_*`) können die Stufe nur enger machen. Stufenwechsel nur von Hand (`LIVE_STAGE`).

## Go-Live-Prüfung (`readiness.py`, `python -m live check`)

Pflicht vor echten Sends (`LIVE_REQUIRE_GATE=true`): Trading-Wallet mit Schlüssel; mindestens ein Modus auf
Echtgeld; je Echtgeld-Modus Papier-Plus nach Kosten über 7 Tage (≥ 30 Trades, netto > 0, Profit-Faktor ≥ 1,2,
beide Wochenhälften im Plus); Dry-Run (≥ 5 simulierte Käufe, ≥ 3 gebaute Verkäufe, ≥ 3 gebuchte Dry-Runden);
Telegram eingerichtet; Live-Wächter läuft; kein Not-Aus, keine unklare Order.

## Not-Aus

- Telegram (nur aus deinem Chat): `/notaus` (keine Käufe mehr), `/alles_verkaufen` (Not-Aus plus alle Live- und
  Dry-Positionen verkaufen), `/weiter`, `/live` (Status).
- Terminal: `python -m live kill`, `python -m live resume`. Dashboard: „Pausieren“ stoppt alle Käufe.
- Automatisch: 3 Fehlschläge in Folge, Tagesverlust-Grenze (rollierend 24 h), Datenausfall (Jupiter 3× nicht
  erreichbar), Live-Wächter tot (Echtgeld).

## Wächter (`service.py`, Dienst `live`)

Alle 30 s Lebenszeichen, unklare Orders klären, Datenausfall prüfen, Verlustalarm; alle 10 min (Echtgeld) Token in
der Wallet ohne Position melden. Nur öffentliche RPC, keine Helius-Credits.

## Schlüssel

`python -m live new-wallet` legt eine eigene Trading-Wallet an (`~/.config/mayhambot/trading-keypair.json`,
chmod 600, außerhalb des Repos) und zeigt nur den öffentlichen Schlüssel. Alternativ macOS-Schlüsselbund
(`LIVE_KEYCHAIN_SERVICE`). Der Schlüssel wird nie geloggt, gespeichert oder verschickt.

## Tabellen

`live_orders` jede Order (auch Dry-Run): Quote, Mindestmenge, Preiseinfluss, Slippage, Priority-Fee, Signatur,
Status (`rejected`, `dry_ok`, `dry_sim_failed`, `dry_built`, `sending`, `landed`, `failed`, `expired`, `sim_failed`,
`unknown`, `landed_late`, `landed_unbooked`), gelandete Mengen, realisierte Slippage.
`live_state` Not-Aus, Datenausfall, Lebenszeichen, Wallet-Pubkey, Telegram-Offset.
