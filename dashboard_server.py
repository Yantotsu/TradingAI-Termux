"""Serve the local web dashboard and read-only bot status on localhost."""
from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

WEB_DIR = Path(__file__).resolve().parent / "web"


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_dashboard_server(status_file: Path, port: int = 8765) -> DashboardServer:
    status_file = status_file.resolve()

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(WEB_DIR), **kwargs)

        def do_GET(self) -> None:
            if urlsplit(self.path).path == "/api/status":
                try:
                    payload = json.loads(status_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    payload = {"running": False, "action": "HOLD", "note": "Bot belum menulis status."}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            self.path = urlsplit(self.path).path or "/"
            if self.path == "/":
                self.path = "/index.html"
            super().do_GET()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = DashboardServer(("127.0.0.1", port), Handler)
    Thread(target=server.serve_forever, name="trading-dashboard-http", daemon=True).start()
    return server
