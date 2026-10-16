"""Tiny in-process ASGI client for the tests: no httpx/httpx2 or other test package needed."""
from __future__ import annotations

import asyncio
import json as _json
from urllib.parse import urlsplit


class Response:
    def __init__(self, status: int, body: bytes):
        self.status_code, self.content = status, body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return _json.loads(self.content)


class Client:
    def __init__(self, app, client=("127.0.0.1", 5000)):
        self.app, self.client = app, client

    def _call(self, method: str, url: str, body: bytes = b"", headers=()) -> Response:
        u = urlsplit(url)
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": u.path, "raw_path": u.path.encode(), "query_string": u.query.encode(),
                 "root_path": "", "server": ("testserver", 80), "client": self.client,
                 "headers": [*([] if any(k == b"host" for k, _ in headers) else [(b"host", b"127.0.0.1:8600")]),
                             (b"content-length", str(len(body)).encode()), *headers]}
        sent = {"done": False}
        status, chunks = [0], []

        async def receive():
            if sent["done"]:
                await asyncio.sleep(3600)
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(msg):
            if msg["type"] == "http.response.start":
                status[0] = msg["status"]
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))

        asyncio.run(self.app(scope, receive, send))
        return Response(status[0], b"".join(chunks))

    def get(self, url: str) -> Response:
        return self._call("GET", url)

    def post(self, url: str, json=None) -> Response:
        return self._call("POST", url, _json.dumps(json or {}).encode(), [(b"content-type", b"application/json")])
