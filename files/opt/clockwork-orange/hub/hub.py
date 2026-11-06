#!/usr/bin/env python3
"""Clockwork Orange Hub - launcher + status panel for the uConsole RF suite.

  hub.py serve          local web UI on 127.0.0.1:8090 (opened as a Chromium app window by co-hub)
  hub.py launch <id>    start an app from apps.json (used by the hub, menu and desktop icons alike)

Binds to localhost only: /api/launch runs programs, so it must never be reachable from the LAN.
Stdlib only, so it works before/without any pip installs.
"""
import json, os, glob, shlex, socket, subprocess, sys, time, threading, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(os.path.dirname(HERE), "icons")
PORT = 8090
RAILS = ("sdr", "gps", "lora", "usb")
SERVICES = {"lg": "sdr-looking-glass", "mesh": "meshtasticd"}   # hub-controllable services (whitelist)
SLUG = os.environ.get("CO_REPO_SLUG", "OzInFl/clockwork-orange-uconsole")   # where releases are published

def load_apps():
    with open(os.path.join(HERE, "apps.json")) as f:
        return json.load(f)

def app_by_id(aid):
    return next((a for a in load_apps()["apps"] if a["id"] == aid), None)

def launch(aid):
    a = app_by_id(aid)
    if not a: return False, "unknown app %s" % aid
    cmd = os.path.expandvars(a["exec"])
    if a.get("sdr"): cmd = "co-sdr-run " + cmd          # frees the RTL from Looking Glass, resumes it after
    if a.get("lora"): cmd = "co-lora-run " + cmd        # frees the SX1262 from meshtasticd, resumes it after
    if a.get("terminal"):
        inner = "%s; echo; read -n1 -s -p '[ press any key to close ]'" % cmd
        argv = ["lxterminal", "--no-remote", "-T", a["name"], "-e", "bash -lc " + shlex.quote(inner)]
    else:
        argv = ["bash", "-lc", cmd]
    subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, "launched %s" % a["name"]

# ------------------------------------------------------------------ status
def _read(path, default=None):
    try:
        with open(path) as f: return f.read().strip()
    except Exception: return default

def _sh(*argv, timeout=3):
    try: return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception: return ""

GPS = {"t": 0, "v": {}}
def gps_status():
    """One short gpsd poll (TPV + SKY), cached a few seconds so status polls stay cheap."""
    if time.time() - GPS["t"] < 4: return GPS["v"]
    v = {"mode": 0}
    try:
        with socket.create_connection(("127.0.0.1", 2947), timeout=1.5) as s:
            s.sendall(b'?WATCH={"enable":true,"json":true}\n'); s.settimeout(1.5)
            buf = b""; t0 = time.time()
            while time.time() - t0 < 2.0 and not ("lat" in v and "sats" in v):
                try: chunk = s.recv(4096)
                except socket.timeout: break                  # gpsd up but quiet (no device/fix yet)
                if not chunk: break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    try: m = json.loads(ln)
                    except Exception: continue
                    if m.get("class") == "TPV":
                        v["mode"] = max(v.get("mode", 0), m.get("mode", 0))
                        for k in ("lat", "lon", "alt", "speed"):
                            if k in m: v[k] = m[k]
                    elif m.get("class") == "SKY" and "satellites" in m:
                        sats = m["satellites"]; v["sats"] = len(sats); v["used"] = sum(1 for x in sats if x.get("used"))
        v["gpsd"] = True
    except Exception:
        v["gpsd"] = False
    GPS.update(t=time.time(), v=v)
    return v

# ------------------------------------------------------------------ versions
# Installed versions come from VERSION files (install.sh writes /opt/clockwork-orange/VERSION, "+dev" when
# not installed from a release tag). The latest published release is checked on GitHub every 6 h.
REL = {"latest": None, "t": 0}
def release_loop():
    while True:
        try:
            req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % SLUG,
                                         headers={"Accept": "application/vnd.github+json", "User-Agent": "co-hub"})
            with urllib.request.urlopen(req, timeout=15) as r:
                REL.update(latest=json.load(r)["tag_name"].lstrip("v"), t=time.time())
            time.sleep(6 * 3600)
        except Exception:
            time.sleep(600)                                  # offline in the field: try again later

def versions():
    return {"suite": _read(os.path.join(os.path.dirname(HERE), "VERSION")),
            "lg": _read(os.path.expanduser("~/sdr-looking-glass/VERSION")),
            "latest": REL["latest"], "checked": REL["t"] or None}

def status():
    rails = {}
    for n in RAILS:
        rails[n] = _sh("co-rail", n, "status").endswith(" on")
    cap = _read("/sys/class/power_supply/axp20x-battery/capacity")
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    return {"time": time.strftime("%H:%M"), "host": socket.gethostname(),
            "ip": (_sh("hostname", "-I").split() or [""])[0],
            "battery": int(cap) if cap and cap.isdigit() else None,
            "charging": _read("/sys/class/power_supply/axp22x-ac/online") == "1",
            "temp": round(int(temp) / 1000, 1) if temp and temp.isdigit() else None,
            "load": _read("/proc/loadavg", "").split(" ")[0],
            "rails": rails, "services": {k: _sh("systemctl", "is-active", u) for k, u in SERVICES.items()},
            "gps": gps_status(), "versions": versions(),
            "apps_mtime": os.path.getmtime(os.path.join(HERE, "apps.json"))}

# ------------------------------------------------------------------ icons
ICON_DIRS = ["/usr/share/icons/hicolor/scalable/apps", "/usr/share/icons/hicolor/256x256/apps",
             "/usr/share/icons/hicolor/128x128/apps", "/usr/share/icons/hicolor/64x64/apps",
             "/usr/share/icons/hicolor/48x48/apps", "/usr/share/pixmaps", "/usr/share/sdrpp/icons",
             "/usr/share/cubicsdr"]
def find_icon(name):
    for d in [ICONS] + ICON_DIRS:
        for ext in (".svg", ".png"):
            p = os.path.join(d, name + ext)
            if os.path.isfile(p): return p
    hits = glob.glob("/usr/share/icons/hicolor/*/apps/%s.*" % name)
    return hits[0] if hits else os.path.join(ICONS, "co-app.svg")

# ------------------------------------------------------------------ http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)): body = json.dumps(body)
        if isinstance(body, str): body = body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f: return self._send(200, f.read(), "text/html; charset=utf-8")
        if p == "/apps.json": return self._send(200, load_apps())
        if p == "/api/status": return self._send(200, status())
        if p.startswith("/icon/"):
            f = find_icon(os.path.basename(p[6:]))
            with open(f, "rb") as fh:
                return self._send(200, fh.read(), "image/svg+xml" if f.endswith(".svg") else "image/png")
        self._send(404, {"err": "not found"})

    def do_POST(self):
        # same-origin only: reject cross-site form posts from any other page open in the browser
        if self.headers.get("Origin") not in (None, "http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT):
            return self._send(403, {"err": "bad origin"})
        parts = self.path.strip("/").split("/")
        if parts[:2] == ["api", "launch"] and len(parts) == 3:
            ok, msg = launch(parts[2]); return self._send(200 if ok else 404, {"ok": ok, "msg": msg})
        if parts[:2] == ["api", "rail"] and len(parts) == 4 and parts[2] in RAILS and parts[3] in ("on", "off"):
            _sh("co-rail", parts[2], parts[3]); GPS["t"] = 0
            return self._send(200, {"ok": True, "msg": "%s rail %s" % (parts[2].upper(), parts[3])})
        if parts[:2] == ["api", "svc"] and len(parts) == 4 and parts[2] in SERVICES and parts[3] in ("start", "stop"):
            _sh("sudo", "systemctl", parts[3], SERVICES[parts[2]], timeout=20)
            return self._send(200, {"ok": True, "msg": "%s %s" % (SERVICES[parts[2]], "started" if parts[3] == "start" else "stopped")})
        self._send(404, {"ok": False, "msg": "unknown action"})

def serve():
    threading.Thread(target=release_loop, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "launch":
        ok, msg = launch(sys.argv[2]); print(msg); sys.exit(0 if ok else 1)
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        serve()
    else:
        print(__doc__); sys.exit(2)
