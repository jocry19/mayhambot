"""python -m brain run | status | decisions [n] | journal [--write] | beliefs"""
import json
import logging
import signal
import sys
import threading


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "status"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from brain import config, store

    s = config.load()
    if cmd == "run":
        from brain.service import Service

        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        Service(s).run(stop)
        return 0
    conn = store.connect(s.db_path)
    import brain

    if cmd == "status":
        print(json.dumps(brain.status(conn), indent=2, ensure_ascii=False, default=str))
    elif cmd == "decisions":
        n = int(argv[1]) if len(argv) > 1 else 10
        for d in reversed(brain.decisions(conn, n)):
            print(f"--- #{d['id']} {d['kind']} {d['decision']}\n{d['note']}\n")
    elif cmd == "journal":
        if "--write" in argv:
            from brain import journal
            from brain.service import load_model

            print(journal.write(conn, load_model(s), s))
        else:
            for e in brain.diary(conn, 3):
                print(e["text"], "\n")
    elif cmd == "beliefs":
        from brain import features, journal
        from brain.service import load_model

        eff = journal.effects(load_model(s), min_evidence=5)
        for t, (e, n) in sorted(eff.items(), key=lambda x: x[1][0]):
            print(f"{e:+.2%}  n={n:7.1f}  {features.describe(t)}  [{t}]")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
