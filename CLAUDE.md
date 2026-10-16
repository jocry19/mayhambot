# mayhambot — notes for Claude sessions on this Mac

- Read README.md first (start/stop, autostart, layout). core/README.md = interfaces shared by signals/ and copytrade/.
- The app runs as a launchd user agent `de.mayhambot.app` (`python mayham.py run`), dashboard `web/` on http://localhost:8600 (127.0.0.1 only), TUI `python -m cli`,
  logs in logs/. Check with `.venv/bin/python mayham.py status`.
- Workflow for changes: edit -> `.venv/bin/python tests/test_logic.py && .venv/bin/python -m pytest tests -q`
  -> `git pull` first, commit only your own package to `main`, push -> on the Mac `.venv/bin/python mayham.py update`.
- Folder of record on the Mac: ~/Documents/WALLET/mayhambot. GitHub is transport only; the bot runs only on the Mac.
- Never commit or print .env, keypair files or private keys. Live trading stays off unless the user turns it on.
- Data is real (DexScreener, GeckoTerminal, Solana RPC, Jupiter); no mocked data in the app.
