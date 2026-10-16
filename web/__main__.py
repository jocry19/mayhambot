"""python -m web  ->  dashboard on http://WEB_HOST:WEB_PORT (default 127.0.0.1:8600)."""
import os
import sys


def main():
    try:  # redact api keys in logs before anything else is imported
        import core.http  # type: ignore  # noqa: F401
    except Exception:
        pass
    try:
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401
    except ImportError:
        sys.exit("Es fehlen Pakete: .venv/bin/pip install -r web/requirements.txt")
    import uvicorn
    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8600"))
    if host not in ("127.0.0.1", "localhost", "::1") and not os.getenv("WEB_PASSWORD"):
        print("WARNUNG: Dashboard ist im Netz erreichbar ohne WEB_PASSWORD; Steuerung nur von localhost erlaubt.")
    print(f"mayhambot Dashboard: http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    uvicorn.run("web.server:app", host=host, port=port, log_level=os.getenv("WEB_LOG_LEVEL", "warning"),
                workers=1, proxy_headers=True)


if __name__ == "__main__":
    main()
