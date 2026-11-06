"""co_web - tiny stdlib web layer shared by the Clockwork Orange apps (pager, spy).

App(port, page) + @app.get / @app.post routes returning dict/list (JSON), (code, dict), or (bytes, ctype).
Live updates: app.push(event_dict) fans out to every /api/stream (SSE) client.
POSTs must come from the app's own page (Origin = Host), so another site open in the browser can't drive the radio.
Binds 0.0.0.0: the UI works on the uConsole screen and from a PC on the LAN, like Looking Glass.
"""
import json, os, queue, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

class App:
    def __init__(self, port, page):
        self.port, self.page, self.routes, self.subs = port, page, {}, []

    def get(self, path):
        def deco(f): self.routes[("GET", path)] = f; return f
        return deco

    def post(self, path):
        def deco(f): self.routes[("POST", path)] = f; return f
        return deco

    def push(self, ev):
        for q in list(self.subs):
            try: q.put_nowait(ev)
            except queue.Full:
                try: self.subs.remove(q)
                except ValueError: pass

    def clients(self): return len(self.subs)

    def serve(self):
        app = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a): pass

            def _send(self, code, body, ctype="application/json", extra=None):
                if isinstance(body, (dict, list)): body = json.dumps(body)
                if isinstance(body, str): body = body.encode()
                self.send_response(code); self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items(): self.send_header(k, v)
                self.end_headers(); self.wfile.write(body)

            def _route(self, method):
                u = urlparse(self.path); q = {k: v[-1] for k, v in parse_qs(u.query).items()}
                if method == "GET" and u.path == "/":
                    with open(app.page, "rb") as f: return self._send(200, f.read(), "text/html; charset=utf-8")
                if method == "GET" and u.path == "/api/stream": return self._stream()
                f = app.routes.get((method, u.path))
                if not f:
                    pre = [k for k in app.routes if k[0] == method and k[1].endswith("/") and u.path.startswith(k[1])]
                    if pre: f = app.routes[pre[0]]; q["_rest"] = u.path[len(pre[0][1]):]
                if not f: return self._send(404, {"ok": False, "err": "not found"})
                body = {}
                if method == "POST":
                    o = self.headers.get("Origin")
                    if o and urlparse(o).netloc != self.headers.get("Host"):
                        return self._send(403, {"ok": False, "err": "bad origin"})
                    n = int(self.headers.get("Content-Length") or 0)
                    try: body = json.loads(self.rfile.read(n) or b"{}")
                    except Exception: body = {}
                try: r = f(q, body) if method == "POST" else f(q)
                except Exception as e: return self._send(500, {"ok": False, "err": "%s: %s" % (type(e).__name__, e)})
                if isinstance(r, tuple) and isinstance(r[0], int): return self._send(r[0], r[1])
                if isinstance(r, tuple): return self._send(200, r[0], r[1], r[2] if len(r) > 2 else None)
                return self._send(200, r)

            def _stream(self):
                q = queue.Queue(maxsize=500); app.subs.append(q)
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store"); self.end_headers()
                try:
                    self.wfile.write(b": hi\n\n"); self.wfile.flush()
                    while True:
                        try: ev = q.get(timeout=15); data = "data: %s\n\n" % json.dumps(ev)
                        except queue.Empty: data = ": ping\n\n"
                        self.wfile.write(data.encode()); self.wfile.flush()
                except Exception: pass
                finally:
                    try: app.subs.remove(q)
                    except ValueError: pass

            def do_GET(self): self._route("GET")
            def do_POST(self): self._route("POST")

        srv = ThreadingHTTPServer(("0.0.0.0", self.port), H); srv.daemon_threads = True
        srv.serve_forever()

def spawn(target, *a):
    t = threading.Thread(target=target, args=a, daemon=True); t.start(); return t

def load_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default

def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f, indent=1)
    os.replace(tmp, path)
