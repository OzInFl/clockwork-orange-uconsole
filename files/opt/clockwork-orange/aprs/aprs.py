#!/usr/bin/env python3
"""Clockwork Orange APRS Monitor - receive-only APRS with a live map, stations, weather, messages and a packet log.

rtl_fm (FM, 22.05 kHz) -> direwolf (1200 bd AFSK modem; prints each frame as "[ch] SRC>DST,PATH:info")
-> aprslib (positions incl. Mic-E + compressed, objects/items, weather, messages, status, telemetry) -> SQLite
(~/co-aprs/aprs.db) + in-memory station table. Distance/bearing from the uConsole's own GPS (gpsd).
Radio from co_radio: a second USB RTL if present, else the AIO RTL borrowed from Looking Glass (resumed on stop).
Web UI + API on :8094 (Leaflet served locally from libjs-leaflet, so the map works offline; tiles need internet).
Receive only: no PTT, no beacons, no igate.
"""
import json, math, os, re, socket, sqlite3, subprocess, sys, threading, time
sys.path.insert(0, "/opt/clockwork-orange/lib"); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import aprslib
import co_radio
from co_web import App, spawn, load_json, save_json

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CO_APRS_PORT") or 8094)
CFG_PATH = os.path.expanduser("~/.config/clockwork-orange/aprs.json")
DATA = os.path.expanduser(os.environ.get("CO_APRS_DATA") or "~/co-aprs")
LEAFLET = "/usr/share/javascript/leaflet"
MAX_ROWS = 300000
DEFAULTS = {"freq": 144.390, "gain": "40", "ppm": None, "mycall": "N0CALL", "record_on_boot": False, "device": None, "max_age_h": 24}

app = App(PORT, os.path.join(HERE, "index.html"))
LOCK = threading.RLock()
CFG = dict(DEFAULTS, **load_json(CFG_PATH, {}))
ST = {"want": False, "gen": 0, "reload": False, "running": False, "error": None, "device": None, "borrowed": False, "since": None,
      "freq": None, "packets": 0, "bad": 0, "last": None, "procs": None, "level": None}
STATIONS = {}          # callsign (or object name) -> station record
ME = {"lat": None, "lon": None, "t": None}

def save_cfg():
    with LOCK: save_json(CFG_PATH, CFG)

# ------------------------------------------------------------------ storage
os.makedirs(DATA, exist_ok=True)
def db():
    c = sqlite3.connect(os.path.join(DATA, "aprs.db"), timeout=10); c.row_factory = sqlite3.Row; return c
with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS packets (id INTEGER PRIMARY KEY, t REAL, src TEXT, dst TEXT, path TEXT, fmt TEXT, name TEXT,
                 lat REAL, lon REAL, direct INTEGER, level INTEGER, raw TEXT, info TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS packets_t ON packets(t)"); c.execute("CREATE INDEX IF NOT EXISTS packets_name ON packets(name)")

# ------------------------------------------------------------------ geometry + GPS
def dist_brg(la1, lo1, la2, lo2):
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    d = 2 * 6371.0 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))
    b = math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)))
    return round(d, 2), round((b + 360) % 360)

def gps_loop():
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", 2947), timeout=5); s.sendall(b'?WATCH={"enable":true,"json":true}\n'); s.settimeout(15)
            buf = b""
            while True:
                ch = s.recv(4096)
                if not ch: break
                buf += ch; *lines, buf = buf.split(b"\n")
                for ln in lines:
                    try: m = json.loads(ln)
                    except Exception: continue
                    if m.get("class") == "TPV" and m.get("mode", 0) >= 2 and m.get("lat") is not None:
                        ME.update(lat=m["lat"], lon=m["lon"], t=time.time())
        except Exception: pass
        time.sleep(10)

# ------------------------------------------------------------------ decoding
_FRAME = re.compile(r"^\[[\d.]+[^\]]*\]\s+(\S+?>\S+?:.*)$")      # direwolf: "[0.4] SRC>DST,PATH:info" (also "[0L] ...")
_LEVEL = re.compile(r"audio level = (\d+)")
_HEX = re.compile(r"<0x([0-9a-fA-F]{2})>")                      # direwolf shows unprintables as <0x0d>

def handle_frame(tnc2, level):
    now = time.time()
    tnc2 = _HEX.sub(lambda m: chr(int(m.group(1), 16)), tnc2)
    head, info = tnc2.split(":", 1)
    src, rest = head.split(">", 1); dst, *path = rest.split(",")
    direct = int(not any(p.endswith("*") for p in path))            # '*' = a digipeater already repeated it
    digi = next((p.rstrip("*") for p in reversed(path) if p.endswith("*")), None)
    try:
        d = aprslib.parse(tnc2)
    except Exception as e:
        d = {"format": "unparsed", "from": src, "comment": info[:120], "err": str(e)[:80]}
    fmt = d.get("format", "?")
    if fmt == "mic-e" and d.get("comment"):                   # drop the radio-model codes Mic-E rigs wrap the comment in
        d["comment"] = re.sub(r'(_[ "#$%()]|\]=?|\^=?|~=?|`_?.?)$', "", re.sub(r"^[`'\]>]", "", d["comment"])).strip()
    if fmt == "bulletin" and not d.get("addresse"): d["addresse"] = "BLN" + str(d.get("bid", "")) + str(d.get("identifier", ""))
    name = d.get("object_name") or d.get("item_name") or src
    name = name.strip()
    lat, lon = d.get("latitude"), d.get("longitude")
    if lat is not None and not (-90 <= lat <= 90 and -180 <= lon <= 180): lat = lon = None
    with LOCK:
        ST["packets"] += 1; ST["last"] = now
        s = STATIONS.setdefault(name, {"name": name, "first": now, "n": 0, "track": [], "digis": {}})
        s.update(n=s["n"] + 1, last=now, src=src, path=",".join(path), direct=direct, level=level, fmt=fmt)
        if digi: s["digis"][digi] = s["digis"].get(digi, 0) + 1
        if name != src: s["kind"] = "object" if d.get("object_name") else "item"; s["by"] = src; s["alive"] = d.get("alive", True)
        for k in ("symbol", "symbol_table", "comment", "altitude", "speed", "course", "phg", "status"):
            if d.get(k) not in (None, ""): s[k] = d[k]
        if d.get("weather"): s["wx"] = d["weather"]; s["wx_t"] = now
        if d.get("telemetry"): s["telemetry"] = d["telemetry"]
        if lat is not None:
            moved = not s.get("lat") or abs(s["lat"] - lat) + abs(s["lon"] - lon) > 1e-4
            s.update(lat=round(lat, 5), lon=round(lon, 5), pos_t=now)
            if moved: s["track"].append((round(lat, 5), round(lon, 5), now)); del s["track"][:-200]
    msg = None
    if fmt in ("message", "bulletin") or d.get("addresse"):
        msg = {"from": src, "to": (d.get("addresse") or "").strip(), "text": d.get("message_text") or d.get("response") or "",
               "no": d.get("msgNo"), "bulletin": fmt == "bulletin" or (d.get("addresse") or "").startswith("BLN"), "t": now,
               "mine": (d.get("addresse") or "").strip().upper().split("-")[0] == str(CFG.get("mycall", "")).upper().split("-")[0]}
    with db() as c:
        c.execute("INSERT INTO packets (t, src, dst, path, fmt, name, lat, lon, direct, level, raw, info) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (now, src, dst, ",".join(path), fmt, name, lat, lon, direct, level, tnc2,
                   json.dumps({k: v for k, v in d.items() if k not in ("raw", "from", "to", "path", "via")}, default=str)[:4000]))
    app.push({"t": "pkt", "pkt": {"t": now, "src": src, "name": name, "fmt": fmt, "raw": tnc2[:300], "direct": direct, "level": level,
                                  "lat": lat, "lon": lon, "sym": (d.get("symbol_table") or "") + (d.get("symbol") or "")}, "msg": msg})

def rebuild():
    """Station table from the last max_age_h of stored packets (so a restart doesn't blank the map)."""
    since = time.time() - float(CFG.get("max_age_h") or 24) * 3600
    with db() as c:
        rows = c.execute("SELECT * FROM packets WHERE t > ? ORDER BY id", (since,)).fetchall()
    for r in rows:
        try: d = json.loads(r["info"] or "{}")
        except Exception: d = {}
        s = STATIONS.setdefault(r["name"], {"name": r["name"], "first": r["t"], "n": 0, "track": [], "digis": {}})
        s.update(n=s["n"] + 1, last=r["t"], src=r["src"], path=r["path"], direct=r["direct"], level=r["level"], fmt=r["fmt"])
        for p in (r["path"] or "").split(","):
            if p.endswith("*"): s["digis"][p.rstrip("*")] = s["digis"].get(p.rstrip("*"), 0) + 1
        if r["name"] != r["src"]: s["kind"] = "object" if d.get("object_name") else "item"; s["by"] = r["src"]; s["alive"] = d.get("alive", True)
        for k in ("symbol", "symbol_table", "comment", "altitude", "speed", "course", "phg", "status"):
            if d.get(k) not in (None, ""): s[k] = d[k]
        if d.get("weather"): s["wx"] = d["weather"]; s["wx_t"] = r["t"]
        if r["lat"] is not None:
            if not s.get("lat") or abs(s["lat"] - r["lat"]) + abs(s["lon"] - r["lon"]) > 1e-4: s["track"].append((r["lat"], r["lon"], r["t"]))
            s.update(lat=r["lat"], lon=r["lon"], pos_t=r["t"])
    for s in STATIONS.values(): del s["track"][:-200]

# ------------------------------------------------------------------ receiver
def dev_ppm(lease):
    if CFG.get("ppm") not in (None, ""): return int(round(float(CFG["ppm"])))
    if lease.dev["aio"]:
        try: return int(round(float(re.search(r"^SDR_PPM=([-\d.]+)", open("/etc/default/sdr-looking-glass").read(), re.M).group(1))))
        except Exception: pass
    return 0

def _dw_conf():
    p = os.path.join(DATA, "direwolf.conf")
    call = re.sub(r"[^A-Z0-9-]", "", str(CFG.get("mycall") or "N0CALL").upper()) or "N0CALL"
    open(p, "w").write("# written by aprs.py - receive only: no PTT, no beacons, no digipeating\nADEVICE stdin null\nCHANNEL 0\nMYCALL %s\nMODEM 1200\n" % call)
    return p

def _pipeline(lease):
    freq = float(CFG["freq"])
    rtl = ["rtl_fm", "-d", str(lease.index), "-f", "%.5fM" % freq, "-M", "fm", "-s", "22050", "-E", "dc", "-p", str(dev_ppm(lease))]
    if str(CFG.get("gain", "")).strip().lower() not in ("", "auto"): rtl += ["-g", str(CFG["gain"])]
    rtl += ["-"]
    # stdbuf -oL: direwolf line-buffered, so frames show as they arrive instead of per 4 KB block
    dw = ["stdbuf", "-oL", "direwolf", "-c", _dw_conf(), "-r", "22050", "-D", "1", "-t", "0", "-q", "d", "-"]
    p1 = subprocess.Popen(rtl, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(dw, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.STDOUT); p1.stdout.close()
    with LOCK: ST.update(running=True, error=None, device=lease.describe(), borrowed=lease.borrowed, since=time.time(), freq=freq, procs=(p1, p2))
    app.push({"t": "status"})
    def errs():
        for raw in p1.stderr:
            s = raw.decode(errors="replace").strip()
            if re.search(r"fail|error|usb_claim|lost", s, re.I):
                with LOCK: ST["error"] = "rtl_fm: " + s[:160]
                app.push({"t": "status"})
    spawn(errs)
    level = None
    for raw in p2.stdout:
        s = raw.decode(errors="replace").rstrip()
        m = _LEVEL.search(s)
        if m: level = int(m.group(1)); ST["level"] = level; continue
        m = _FRAME.match(s)
        if m:
            try: handle_frame(m.group(1), level)
            except Exception as e:
                with LOCK: ST["bad"] += 1
                app.push({"t": "log", "msg": "packet error: %s" % e})
            level = None
    for p in (p1, p2):
        try: p.terminate(); p.wait(3)
        except Exception:
            try: p.kill()
            except Exception: pass
    with LOCK: ST.update(running=False, procs=None)

def _loop(gen):
    live = lambda: ST["want"] and ST["gen"] == gen
    back = 2; lease = None
    try:
        while live():
            if lease is None:
                lease, why = co_radio.lease("APRS Monitor", prefer=CFG.get("device"))
                if not lease:
                    with LOCK: ST["error"] = why
                    app.push({"t": "status"}); time.sleep(back); back = min(back * 2, 30); continue
            t0 = time.time(); _pipeline(lease)
            if not live(): break
            if ST.pop("reload", False): continue
            if time.time() - t0 > 30: back = 2
            if not ST["error"]:
                with LOCK: ST["error"] = "receiver stopped; restarting"
            app.push({"t": "status"}); time.sleep(back); back = min(back * 2, 60)
    finally:
        if lease: lease.release()
        with LOCK: ST.update(borrowed=False, device=None)
        app.push({"t": "status"})

def _kill():
    for p in (ST.get("procs") or ()):
        try: p.terminate()
        except Exception: pass

def start():
    with LOCK:
        if ST["want"]: return
        ST["gen"] += 1; ST.update(want=True, error=None); gen = ST["gen"]
    spawn(_loop, gen)

def stop():
    with LOCK: ST["want"] = False
    _kill()

def prune_loop():
    while True:
        try:
            with db() as c:
                n = c.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
                if n > MAX_ROWS: c.execute("DELETE FROM packets WHERE id IN (SELECT id FROM packets ORDER BY id LIMIT ?)", (n - MAX_ROWS,))
            cut = time.time() - float(CFG.get("max_age_h") or 24) * 3600
            with LOCK:
                for k in [k for k, s in STATIONS.items() if s["last"] < cut]: del STATIONS[k]
        except Exception: pass
        time.sleep(600)

# ------------------------------------------------------------------ API
def status():
    with LOCK: s = {k: ST.get(k) for k in ("want", "running", "error", "device", "borrowed", "since", "freq", "packets", "bad", "last", "level")}
    s["radios"] = co_radio.status(); s["me"] = ME; return s

def station_list():
    out = []
    with LOCK:
        for s in STATIONS.values():
            r = {k: v for k, v in s.items() if k != "track"}
            r["track_n"] = len(s["track"])
            if s.get("lat") is not None and ME["lat"] is not None: r["km"], r["brg"] = dist_brg(ME["lat"], ME["lon"], s["lat"], s["lon"])
            out.append(r)
    return sorted(out, key=lambda r: -r["last"])

@app.get("/api/state")
def api_state(q):
    with db() as c: tot = c.execute("SELECT COUNT(*) FROM packets").fetchone()[0]
    return {"status": status(), "cfg": CFG, "stored": tot}

@app.get("/api/stations")
def api_stations(q): return station_list()

@app.get("/api/station")
def api_station(q):
    n = q.get("name", "")
    with LOCK: s = STATIONS.get(n)
    with db() as c: rows = [dict(r) for r in c.execute("SELECT id, t, src, path, fmt, direct, level, raw FROM packets WHERE name=? ORDER BY id DESC LIMIT 100", (n,))]
    return {"station": s, "packets": rows}

@app.get("/api/packets")
def api_packets(q):
    lim = max(1, min(1000, int(q.get("limit") or 200))); w, a = [], []
    if q.get("q"): w.append("(raw LIKE ? OR name LIKE ?)"); a += ["%" + q["q"] + "%"] * 2
    if q.get("fmt"): w.append("fmt=?"); a.append(q["fmt"])
    if q.get("direct") == "1": w.append("direct=1")
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id, t, src, dst, path, fmt, name, lat, lon, direct, level, raw FROM packets %s ORDER BY id DESC LIMIT %d"
                                           % (("WHERE " + " AND ".join(w)) if w else "", lim), a)]

@app.get("/api/messages")
def api_messages(q):
    with db() as c:
        rows = c.execute("SELECT t, src, info FROM packets WHERE fmt IN ('message','bulletin') ORDER BY id DESC LIMIT 300").fetchall()
    out, me = [], str(CFG.get("mycall", "")).upper().split("-")[0]
    for r in rows:
        try: d = json.loads(r["info"])
        except Exception: continue
        to = (d.get("addresse") or ("BLN%s%s" % (d.get("bid", ""), d.get("identifier", "")) if d.get("format") == "bulletin" else "")).strip()
        if d.get("format") == "message" and d.get("response") in ("ack", "rej") and not d.get("message_text"): continue   # acks: noise here
        out.append({"t": r["t"], "from": r["src"], "to": to, "text": d.get("message_text") or "", "no": d.get("msgNo"),
                    "bulletin": d.get("format") == "bulletin" or to.startswith("BLN"), "mine": to.upper().split("-")[0] == me})
    return out

@app.get("/leaflet/")
def api_leaflet(q):
    rel = q.get("_rest", "")
    f = os.path.realpath(os.path.join(LEAFLET, rel))
    if not f.startswith(os.path.realpath(LEAFLET) + os.sep) or not os.path.isfile(f): return 404, {"ok": False}
    ct = {"js": "application/javascript", "css": "text/css", "png": "image/png", "svg": "image/svg+xml"}.get(f.rsplit(".", 1)[-1], "application/octet-stream")
    with open(f, "rb") as fh: return fh.read(), ct

@app.post("/api/start")
def api_start(q, b):
    if b.get("freq"): CFG["freq"] = float(b["freq"]); save_cfg()
    start(); return {"ok": True}

@app.post("/api/stop")
def api_stop(q, b): stop(); return {"ok": True}

SETTABLE = {"freq": float, "gain": str, "ppm": lambda v: None if v in (None, "") else float(v), "mycall": lambda v: str(v).upper().strip()[:9],
            "record_on_boot": bool, "device": lambda v: v or None, "max_age_h": float}

@app.post("/api/settings")
def api_settings(q, b):
    radio = False
    for k, f in SETTABLE.items():
        if k in b:
            try: v = f(b[k])
            except (TypeError, ValueError): continue
            if CFG.get(k) != v: CFG[k] = v; radio = radio or k in ("freq", "gain", "ppm", "mycall", "device")
    save_cfg()
    if radio and ST["want"]: ST["reload"] = True; _kill()
    return {"ok": True, "cfg": CFG}

@app.post("/api/clear")
def api_clear(q, b):
    with db() as c: c.execute("DELETE FROM packets")
    with LOCK: STATIONS.clear(); ST["packets"] = 0
    return {"ok": True}

if __name__ == "__main__":
    rebuild()
    spawn(gps_loop); spawn(prune_loop)
    if CFG.get("record_on_boot"): start()
    app.serve()
