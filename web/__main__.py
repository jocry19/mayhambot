"""python -m web  ->  dashboard on http://WEB_HOST:WEB_PORT (default 127.0.0.1:8600).

Logs go to stdout with timestamps, line by line, so the launcher's logs/web.log fills live.
Access log: every request that changes something, every error (4xx/5xx) and every page load are logged;
the dashboard's own polling (successful GET /api/...) is left out unless WEB_ACCESS_LOG=all.
"""
import logging
import os
import re
import sys

_POLL = re.compile(r'"GET /api/\S* HTTP/[\d.]+" (\d{3})')


class _QuietPolling(logging.Filter):
    def filter(self, rec: logging.LogRecord) -> bool:
        if os.getenv("WEB_ACCESS_LOG", "") == "all":
            return True
        # core.http has already formatted the record (args folded into msg), so match the text
        m = _POLL.search(rec.getMessage())
        return not (m and int(m.group(1)) < 400)


def _log_config(level: str) -> dict:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"quiet_polling": {"()": _QuietPolling}},
        "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                                 "datefmt": "%Y-%m-%d %H:%M:%S"}},
        "handlers": {
            "out": {"class": "logging.StreamHandler", "formatter": "plain", "stream": "ext://sys.stdout"},
            "access": {"class": "logging.StreamHandler", "formatter": "plain", "stream": "ext://sys.stdout",
                       "filters": ["quiet_polling"]},
        },
        "loggers": {
            "uvicorn": {"handlers": ["out"], "level": level, "propagate": False},
            "uvicorn.error": {"handlers": ["out"], "level": level, "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            "web": {"handlers": ["out"], "level": level, "propagate": False},
        },
    }


def main():
    try:  # redact api keys in logs before anything else is imported
        import core.http  # type: ignore  # noqa: F401
    except Exception:
        pass
    try:  # under the launcher stdout is a file: write each line immediately
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    try:
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401
    except ImportError:
        sys.exit("Es fehlen Pakete: .venv/bin/pip install -r requirements.txt")
    import uvicorn
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8600"))
    level = os.getenv("WEB_LOG_LEVEL", "info").upper()
    if host not in ("127.0.0.1", "localhost", "::1") and not os.getenv("WEB_PASSWORD"):
        print("WARNUNG: Dashboard ist im Netz erreichbar ohne WEB_PASSWORD; Steuerung nur von localhost erlaubt.")
    print(f"mayhambot Dashboard: http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    uvicorn.run("web.server:app", host=host, port=port, log_config=_log_config(level), access_log=True,
                workers=1, proxy_headers=True)


if __name__ == "__main__":
    main()
