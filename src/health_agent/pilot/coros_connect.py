"""Single-user loopback consent flow; callback codes never enter access logs."""

import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from health_agent.pilot.coros_auth import CorosAuthError, CorosOAuth


def connect(root: Path, *, port: int = 0, timeout_seconds: int = 1800) -> bool:
    auth = CorosOAuth(root)
    completed = False
    redirect = f"http://127.0.0.1:{port}/callback"

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            nonlocal completed
            if urlsplit(self.path).path != "/callback":
                self.send_error(404)
                return
            try:
                auth.exchange_callback(redirect + "?" + urlsplit(self.path).query)
                completed = True
                status = 200
                text = "COROS подключён. Можно закрыть эту вкладку."
            except (CorosAuthError, OSError, ValueError):
                status = 400
                text = "Подключение не завершено. Вернитесь к странице авторизации и попробуйте ещё раз."
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", port), Callback) as server:
        redirect = f"http://127.0.0.1:{server.server_port}/callback"
        server.timeout = 1
        url = auth.authorization_url(redirect)
        if not webbrowser.open(url):
            raise CorosAuthError("Could not open the COROS authorization browser")
        print("COROS authorization opened; waiting for browser consent.", flush=True)
        deadline = time.monotonic() + timeout_seconds
        while not completed and time.monotonic() < deadline:
            server.handle_request()
    return completed
