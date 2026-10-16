"""python -m model retrain [--loop] | service | tiers [--tier T] | score MINT | versions | report"""
import argparse
import json
import logging

from model.config import TIER_LABELS


def main():
    ap = argparse.ArgumentParser(prog="python -m model")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("retrain")
    r.add_argument("--loop", action="store_true")
    r.add_argument("--every-hours", type=float)
    sub.add_parser("service", help="discovery + retraining + outcome tracking, runs forever")
    t = sub.add_parser("tiers")
    t.add_argument("--tier", choices=["ultra", "smart", "good"])
    t.add_argument("--limit", type=int, default=30)
    s = sub.add_parser("score")
    s.add_argument("mint")
    s.add_argument("--store", action="store_true")
    sub.add_parser("versions")
    sub.add_parser("report")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from model import train
    from research.store import connect
    if a.cmd == "retrain":
        if a.loop:
            train.loop(a.every_hours)
        else:
            print("Modell-Version:", train.run_once())
    elif a.cmd == "service":
        train.loop(discover=True)
    elif a.cmd == "tiers":
        from model import wallet_tiers
        for w in wallet_tiers(tier=a.tier, limit=a.limit):
            s_ = w["stats"]
            print(f"{TIER_LABELS[w['tier']]:12} {w['score']:5.1f}  {w['address']}  coins={s_['n_tokens']} "
                  f"win={s_['win_rate']:.0%} roi={s_['roi']:+.0%} pnl={s_['total_pnl_usd']:,.0f}$ "
                  f"follower={s_['influence_followers']}")
    elif a.cmd == "score":
        from model import score_token
        print(json.dumps(score_token(a.mint, store=a.store), indent=1, default=float))
    elif a.cmd == "versions":
        for v in connect().execute("SELECT id, ts, active, n_events, n_wallets, tier_counts_json, notes FROM model_versions ORDER BY id"):
            print(dict(v))
    elif a.cmd == "report":
        r_ = connect().execute("SELECT markdown FROM research_reports ORDER BY id DESC LIMIT 1").fetchone()
        print(r_[0] if r_ else "Noch kein Report. Erst: python -m model retrain")


if __name__ == "__main__":
    main()
