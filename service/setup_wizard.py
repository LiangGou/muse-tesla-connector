#!/usr/bin/env python3
"""One-time loopback setup wizard: receives the Tesla Client Secret from the
owner's browser paste and stores it in the broker secret store (mode 600).
Binds 127.0.0.1 only, token-gated path, exits after one successful POST or
after the timeout. The secret value is never logged or printed."""
import os
import secrets as _secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

TOKEN = _secrets.token_hex(16)
PORT = 8791
# The wizard writes to the same store the broker reads: TESLA_SECRETS_DIR
# when set, otherwise service/secrets/ next to this script.
STORE = Path(os.environ.get("TESLA_SECRETS_DIR") or (Path(__file__).resolve().parent / "secrets"))
DEADLINE = time.time() + 1800  # 30 minutes

PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Muse Tesla Connector setup</title></head>
<body style="font-family: sans-serif; margin: 2em; max-width: 34em;">
<h1>Muse Tesla Connector — Client Secret setup</h1>
<p>Paste the Client Secret from the Tesla developer portal (your app,
Credentials &amp; APIs tab) into the field below and submit. It is stored
only on this machine, in the connector's private secret store. This page
works exactly once.</p>
<form method="POST" action="/submit-{token}">
<label>Client Secret<br>
<input type="password" name="secret" autocomplete="off"
 style="width: 100%; font-size: 18px; padding: 6px;"></label>
<br><br>
<button type="submit" style="font-size: 18px; padding: 8px 20px;">Store secret</button>
</form>
</body></html>"""

DONE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Done</title></head>
<body style="font-family: sans-serif; margin: 2em;">
<h1>Secret stored</h1>
<p>You can close this tab and hand the browser back. Tell Muse you are done.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # never log paths or bodies
        pass

    def do_GET(self):
        if self.path == f"/setup-{TOKEN}":
            body = PAGE.format(token=TOKEN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != f"/submit-{TOKEN}":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        value = parse_qs(raw).get("secret", [""])[0].strip()
        if len(value) < 10:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Secret looks too short; nothing stored.")
            return
        STORE.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = STORE / "client-secret.txt"
        target.write_text(value + "\n", encoding="utf-8")
        target.chmod(0o600)
        (STORE / ".received").write_text("received\n", encoding="utf-8")
        body = DONE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _shutdown_soon()


def _shutdown_soon():
    """Exit shortly after the response is delivered (one-shot wizard)."""
    threading.Thread(target=_shutdown, daemon=True).start()


def _shutdown():
    time.sleep(0.5)
    server.shutdown()


server = HTTPServer(("127.0.0.1", PORT), Handler)
print(f"WIZARD_URL=http://127.0.0.1:{PORT}/setup-{TOKEN}", flush=True)
while time.time() < DEADLINE:
    server.timeout = 5
    server.handle_request()
    if (STORE / ".received").exists():
        break
if (STORE / ".received").exists():
    print("wizard exit: secret stored", flush=True)
else:
    print("wizard exit: timed out after 30 minutes; nothing stored", flush=True)
