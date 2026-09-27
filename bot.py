"""magicpin AI Challenge — Vera bot.

Run (no dependencies needed):   python bot.py            # listens on $PORT or 8080
Or with uvicorn if you prefer:  uvicorn bot:app --host 0.0.0.0 --port 8080

Also exposes compose(category, merchant, trigger, customer) exactly as the brief (§7.1) asks.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vera.composer import compose as _compose
from vera.engine import handle


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None, universe: list | None = None) -> dict:
    """Brief §7.1 contract: returns body, cta, send_as, suppression_key, rationale (+ template fields).
    `universe` (optional) = all merchant contexts, used for anonymous peer averages (>= 3 merchants)."""
    out = _compose(category, merchant, trigger, customer, universe=universe)
    return {k: v for k, v in out.items() if not k.startswith("_")}


# ------------------------------------------------------------------ stdlib HTTP server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            val = json.loads(raw.decode("utf-8"))
            return val if isinstance(val, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def do_GET(self):
        self._send(*handle("GET", self.path, {}))

    def do_POST(self):
        body = self._body()
        if body is None:
            return self._send(400, {"accepted": False, "reason": "malformed_json"})
        self._send(*handle("POST", self.path, body))

    def log_message(self, fmt, *args):
        if os.environ.get("QUIET") != "1":
            sys.stderr.write("[vera] " + (fmt % args) + "\n")


# ------------------------------------------------------------------ minimal ASGI app (for uvicorn)
async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"}); return
    raw = b""
    while True:
        msg = await receive()
        raw += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    body = {}
    if raw:
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
    if body is None or not isinstance(body, dict):
        status, obj = 400, {"accepted": False, "reason": "malformed_json"}
    else:
        import asyncio
        status, obj = await asyncio.get_running_loop().run_in_executor(None, handle, scope["method"], scope["path"], body)
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"Vera bot listening on :{port}  (LLM: {'on' if os.environ.get('LLM_API_KEY') else 'off — template mode'})", flush=True)
    srv.serve_forever()
