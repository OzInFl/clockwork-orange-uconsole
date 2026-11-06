#!/usr/bin/env python3
"""Clockwork Orange remote console - the uConsole screen + app launcher in a browser (https://<ip>:6080).

Serves the launcher page and noVNC over HTTPS (noVNC's RSA-AES needs a secure context), relays the
/websockify WebSocket to websockify on 127.0.0.1:6081 (-> wayvnc :5900, PAM login), and launches apps
from hub/apps.json on the uConsole's screen. Only app ids in apps.json can be launched (same as a Hub
tile); rails and services stay Hub-only. POSTs need Origin = this page. Stdlib only.
"""
import json, os, socket, ssl, subprocess, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
HUB = "/opt/clockwork-orange/hub"
sys.path.insert(0, HUB); import hub                       # load_apps, find_icon, launch
PORT = int(os.environ.get("CO_MIRROR_PORT") or 6080)
WS = ("127.0.0.1", 6081)
CERT = os.environ.get("CO_MIRROR_CERT") or "/etc/clockwork-orange/novnc.pem"

def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except OSError: pass
    for s in (a, b):
        try: s.shutdown(socket.SHUT_RDWR)
        except OSError: pass

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=HERE, **k)
    def log_message(self, *a): pass
    def end_headers(self): self.send_header("Cache-Control", "no-store"); super().end_headers()

    def _json(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith("/websockify") and self.headers.get("Upgrade", "").lower() == "websocket":
            return self._relay()
        if p == "/apps.json": return self._json(200, hub.load_apps())
        if p.startswith("/icon/"):
            f = hub.find_icon(os.path.basename(p[6:]))
            with open(f, "rb") as fh: b = fh.read()
            self.send_response(200); self.send_header("Content-Type", "image/svg+xml" if f.endswith(".svg") else "image/png")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); return self.wfile.write(b)
        if p in ("/mirror.py",): return self.send_error(404)
        return super().do_GET()

    def _relay(self):
        try: up = socket.create_connection(WS, timeout=5)
        except OSError: return self.send_error(502, "websockify not running")
        up.settimeout(None)
        head = self.requestline + "\r\n" + "".join("%s: %s\r\n" % kv for kv in self.headers.items()) + "\r\n"
        up.sendall(head.encode("latin-1"))
        self.wfile.flush(); c = self.connection
        t = threading.Thread(target=pipe, args=(up, c), daemon=True); t.start()
        pipe(c, up); t.join(); self.close_connection = True

    def do_POST(self):
        host = self.headers.get("Host", "")
        if self.headers.get("Origin") != "https://" + host: return self._json(403, {"ok": False, "msg": "bad origin"})
        parts = self.path.strip("/").split("/")
        if parts[:2] == ["api", "launch"] and len(parts) == 3:
            ok, msg = hub.launch(parts[2]); return self._json(200 if ok else 404, {"ok": ok, "msg": msg})
        self._json(404, {"ok": False, "msg": "unknown action"})

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H); srv.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(CERT)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)  # handshake in the handler thread
    srv.serve_forever()
