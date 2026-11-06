#!/usr/bin/env python3
"""Clockwork Orange ALPR Watch - Flock Safety / Raven detector + known-camera proximity alerts (:8095).

Two independent sources, both passive:
 1. RF signatures heard by the uConsole's own Bluetooth + Wi-Fi (co_wireless). Signatures come from the flock-you project
    (github.com/colonelpanichacks/flock-you), mostly from a Flock camera firmware dump, plus the IEEE OUI registry.
    Graded: strong (alerts), medium (alerts unless turned off), weak (logged only: community OUI list, mostly generic
    chip-vendor prefixes, so false positives are common).
 2. Known camera locations from OpenStreetMap (surveillance:type=ALPR, the data DeFlock maps), downloaded around the
    current position into ~/co-alpr/known.json so alerts work offline. GPS proximity alert when one is ahead.
Every hit is logged with position to ~/co-alpr/alpr.db (CSV / KML export). Config ~/.config/clockwork-orange/alpr.json.
The built-in Wi-Fi can't do monitor mode: camera probe requests aren't visible, only their SoftAP beacons.
"""
import json, os, re, sqlite3, sys, threading, time, urllib.parse, urllib.request
sys.path.insert(0, "/opt/clockwork-orange/lib"); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from co_web import App, spawn, load_json, save_json
import co_wireless as W

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser("~/co-alpr"); os.makedirs(DATA, exist_ok=True)
CFGF = os.path.expanduser("~/.config/clockwork-orange/alpr.json")
KNOWNF = os.path.join(DATA, "known.json")
LEAFLET = "/usr/share/javascript/leaflet"
app = App(int(os.environ.get("CO_ALPR_PORT") or 8095), os.path.join(HERE, "index.html"))
DEF = {"ble": True, "wifi": True, "wifi_every_s": 20, "alert_medium": True, "sound": True, "volume": .8,
       "near_m": 300, "dl_km": 40, "rearm_s": 600}
CFG = dict(DEF, **load_json(CFGF, {}))
LOCK = threading.Lock()
GPS = W.Gps()
SND = {k: os.path.join(DATA, "sounds", k + ".wav") for k in ("rf", "near")}

# ------------------------------------------------------------------ signatures
FLOCK_OUIS = {"B41E52": "Flock Safety (IEEE)", "D411D6": "ShotSpotter / SoundThinking (IEEE)"}
QCA_OUI = "00037F"           # Qualcomm Atheros default MAC baked into the Flock QCA9377 firmware
COMMUNITY_OUIS = set("""70c94e 3c9180 d8f3bc 803049 b83532 145afc 744ca1 083a88 9c2f9d c03532 940853 e4aaea f46add e00af6 24b2b9
00f48d d03957 e8d0fc e04f43 b81ea4 700894 588e81 ec1bbd 3c71bf 5800e3 9035ea 5c93a2 646e69 4827ea a4cf12 14b5cd 826bf2""".upper().split())
FLOCK_GATT = "e8ccbb38-9532-46a8-9fe5-1814df172e6f"
NORDIC_DFU = "00001530-1212-efde-1523-785feabcd123"
RANK = {"weak": 1, "medium": 2, "strong": 3}

def _oui(mac): return mac.replace(":", "").upper()[:6]

def match_ble(a):
    why, name, mac = [], a.get("name") or "", a["addr"]
    if re.fullmatch(r"(?i)penguin-\d{10}", name): why.append(("strong", "Penguin battery-pack name (Flock solar camera)"))
    if name.lower() == "fs ext battery": why.append(("strong", "'FS Ext Battery' (Flock external battery)"))
    if FLOCK_GATT in a["uuids"]: why.append(("strong", "Flock accessory GATT service"))
    rv = [W.short_uuid(u) for u in a["uuids"]]
    if any(s is not None and 0x3100 <= s <= 0x3500 for s in rv): why.append(("strong", "Raven gunshot-detector GATT service (0x3100-0x3500)"))
    if _oui(mac) in FLOCK_OUIS and not W.randomized(mac): why.append(("strong", "MAC vendor " + FLOCK_OUIS[_oui(mac)]))
    if 0x09C8 in a["mfr"]: why.append(("medium", "manufacturer ID 0x09C8 (XUNTONG, Penguin battery chip)"))
    if re.fullmatch(r"\d{10}", name): why.append(("medium", "bare 10-digit serial name (Penguin battery firmware)"))
    if re.search(r"(?i)flock|penguin|pigvision", name) and not why: why.append(("medium", "name contains Flock/Penguin/Pigvision"))
    if name == "DfuTarg" and NORDIC_DFU in a["uuids"]: why.append(("weak", "Nordic DFU target (Penguin battery updating, or any nRF device)"))
    if _oui(mac) in COMMUNITY_OUIS and not W.randomized(mac): why.append(("weak", "community Flock OUI list (generic chip vendor too)"))
    return why

def match_wifi(ap):
    why, ssid, mac = [], ap.get("ssid") or "", ap["bssid"]
    if re.fullmatch(r"Flock-[0-9A-Fa-f]{6}", ssid): why.append(("strong", "Flock camera SoftAP SSID Flock-XXXXXX"))
    elif ssid == "Flock": why.append(("strong", "bare 'Flock' SSID (provisioned camera)"))
    elif re.search(r"(?i)flock|penguin|pigvision|fs ext battery", ssid): why.append(("medium", "SSID contains Flock/Penguin/Pigvision"))
    if _oui(mac) in FLOCK_OUIS: why.append(("strong", "BSSID vendor " + FLOCK_OUIS[_oui(mac)]))
    if _oui(mac) == QCA_OUI: why.append(("medium" if why else "weak", "Qualcomm Atheros default MAC (Flock QCA9377 radio)"))
    if _oui(mac) in COMMUNITY_OUIS: why.append(("weak", "community Flock OUI list (generic chip vendor too)"))
    return why

# ------------------------------------------------------------------ detections
DEV = {}         # key -> {src, addr, name, conf, why[], first, last, n, rssi, best_rssi, lat, lon}
def db():
    c = sqlite3.connect(os.path.join(DATA, "alpr.db"), timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS hits (id INTEGER PRIMARY KEY, t REAL, src TEXT, addr TEXT, name TEXT, conf TEXT, why TEXT, rssi REAL, lat REAL, lon REAL, raw TEXT)")
    c.execute("CREATE INDEX IF NOT EXISTS hits_t ON hits(t)"); return c

def load_recent():
    with db() as c:
        for t, src, addr, name, conf, why, rssi, lat, lon in c.execute(
                "SELECT t,src,addr,name,conf,why,rssi,lat,lon FROM hits WHERE t > ? ORDER BY t", (time.time() - 7 * 86400,)):
            d = DEV.setdefault(src + ":" + addr, {"src": src, "addr": addr, "first": t, "n": 0, "best_rssi": -999, "alerted": t})
            d.update(name=name, conf=conf, why=json.loads(why), last=t, rssi=rssi); d["n"] += 1
            if rssi is not None and rssi >= d["best_rssi"]: d.update(best_rssi=rssi, lat=lat, lon=lon)

LOGGED = {}      # key -> last db write (one row per device per 30 s, so a parked camera doesn't flood the db)
def hit(src, addr, name, why, rssi, raw):
    conf = max((w[0] for w in why), key=RANK.get)
    fx = GPS.fix(); now = time.time(); key = src + ":" + addr
    with LOCK:
        d = DEV.get(key); new = d is None
        if new: d = DEV[key] = {"src": src, "addr": addr, "first": now, "n": 0, "best_rssi": -999, "alerted": 0}
        d.update(name=name, conf=conf, why=[w[1] for w in why], last=now, rssi=rssi); d["n"] += 1
        if rssi is not None and rssi >= d["best_rssi"]:
            d["best_rssi"] = rssi
            if fx: d.update(lat=fx["lat"], lon=fx["lon"])
        alert = RANK[conf] >= (2 if CFG["alert_medium"] else 3) and now - d["alerted"] > CFG["rearm_s"]
        if alert: d["alerted"] = now
    if now - LOGGED.get(key, 0) > 30 or new:
        LOGGED[key] = now
        with db() as c:
            c.execute("INSERT INTO hits (t,src,addr,name,conf,why,rssi,lat,lon,raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (now, src, addr, name, conf, json.dumps(d["why"]), rssi, fx and fx["lat"], fx and fx["lon"], raw))
    if alert:
        if CFG["sound"]: W.play(SND["rf"], CFG["volume"])
        app.push({"t": "alert", "kind": "rf", "d": pub(d)})
    app.push({"t": "dev", "d": pub(d)})

def pub(d): return {k: v for k, v in d.items() if k != "alerted"}

def on_ble(a):
    if not CFG["ble"]: return
    why = match_ble(a)
    if why: hit("ble", a["addr"], a.get("name"), why, a.get("rssi"), W.adv_hex(a))

STAT = {"wifi_aps": 0, "near": []}
def wifi_loop():
    while True:
        if CFG["wifi"]:
            try:
                aps = W.wifi_scan(); STAT["wifi_aps"] = len(aps)
                for ap in aps:
                    why = match_wifi(ap)
                    if why: hit("wifi", ap["bssid"], ap.get("ssid"), why, ap.get("signal"), "ch %s %s" % (ap.get("chan"), ap.get("sec")))
            except Exception as e: W.WIFI.update(ok=False, err=str(e))
        time.sleep(max(10, int(CFG["wifi_every_s"])))

# ------------------------------------------------------------------ known cameras (OpenStreetMap / DeFlock)
KNOWN = load_json(KNOWNF, {"t": 0, "center": None, "km": 0, "cams": []})
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]

def fetch_known(lat, lon, km):
    q = '[out:json][timeout:90];node["surveillance:type"="ALPR"](around:%d,%.5f,%.5f);out body;' % (km * 1000, lat, lon)
    err = None
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "clockwork-orange-uconsole ALPR Watch"})
            with urllib.request.urlopen(req, timeout=120) as r: js = json.load(r)
            break
        except Exception as e: err = "%s: %s" % (url.split("/")[2], e)
    else: raise RuntimeError(err)
    cams = []
    for e in js.get("elements", []):
        t = e.get("tags", {})
        dr = t.get("camera:direction") or t.get("direction")
        try: dr = float(str(dr).split(";")[0])
        except (TypeError, ValueError): dr = None
        cams.append({"id": e["id"], "lat": e["lat"], "lon": e["lon"], "dir": dr,
                     "maker": t.get("manufacturer") or t.get("brand"), "op": t.get("operator"), "name": t.get("name")})
    KNOWN.update(t=time.time(), center=[round(lat, 3), round(lon, 3)], km=km, cams=cams); save_json(KNOWNF, KNOWN)
    return len(cams)

NEAR_ALERTED = {}   # cam id -> t
def near_loop():
    """Once a second: known cameras within near_m, nearest first; alert once per pass (re-armed after 2x distance)."""
    while True:
        time.sleep(1)
        fx = GPS.fix(); out = []
        if fx and KNOWN["cams"]:
            la, lo = fx["lat"], fx["lon"]; dlat = CFG["near_m"] * 3 / 111000.0
            for c in KNOWN["cams"]:
                if abs(c["lat"] - la) > dlat or abs(c["lon"] - lo) > dlat * 2: continue
                d = W.dist_m(la, lo, c["lat"], c["lon"])
                if d > CFG["near_m"] * 2:
                    NEAR_ALERTED.pop(c["id"], None); continue
                b = W.bearing(la, lo, c["lat"], c["lon"])
                moving = (fx.get("speed") or 0) > 2 and fx.get("track") is not None
                ahead = moving and abs((b - fx["track"] + 180) % 360 - 180) < 70
                facing = c["dir"] is not None and abs((W.bearing(c["lat"], c["lon"], la, lo) - c["dir"] + 180) % 360 - 180) < 45
                out.append(dict(c, m=round(d), brg=round(b), ahead=ahead, facing=facing))
                if d <= CFG["near_m"] and c["id"] not in NEAR_ALERTED and (ahead or not moving):
                    NEAR_ALERTED[c["id"]] = time.time()
                    if CFG["sound"]: W.play(SND["near"], CFG["volume"])
                    app.push({"t": "alert", "kind": "near", "d": out[-1]})
        out.sort(key=lambda x: x["m"]); STAT["near"] = out[:20]
        if app.clients(): app.push({"t": "tick", "d": tick()})

def tick():
    fx = GPS.fix()
    return {"gps": fx, "near": STAT["near"], "ble": {k: W.BLE[k] for k in ("ok", "err", "t", "n")},
            "wifi": {k: W.WIFI[k] for k in ("ok", "err", "t", "iface")}, "wifi_aps": STAT["wifi_aps"], "now": time.time()}

# ------------------------------------------------------------------ API
@app.get("/api/state")
def api_state(q):
    with LOCK: devs = sorted((pub(d) for d in DEV.values()), key=lambda d: -d["last"])
    k = {x: KNOWN[x] for x in ("t", "center", "km")}; k["n"] = len(KNOWN["cams"])
    return {"cfg": CFG, "devs": devs, "known": k, "tick": tick()}

@app.get("/api/known")
def api_known(q): return KNOWN["cams"]

@app.post("/api/known/fetch")
def api_fetch(q, b):
    fx = GPS.fix(600)
    lat, lon = (b.get("lat"), b.get("lon")) if b.get("lat") is not None else ((fx or {}).get("lat"), (fx or {}).get("lon"))
    if lat is None: return 400, {"ok": False, "err": "no GPS fix: wait for one, or pick a spot on the map"}
    km = max(5, min(150, float(b.get("km") or CFG["dl_km"])))
    try: n = fetch_known(float(lat), float(lon), km)
    except Exception as e: return 502, {"ok": False, "err": "download failed (needs internet): %s" % e}
    return {"ok": True, "n": n}

@app.post("/api/cfg")
def api_cfg(q, b):
    for k, v in b.items():
        if k in DEF: CFG[k] = type(DEF[k])(v)
    save_json(CFGF, CFG); return {"ok": True, "cfg": CFG}

@app.post("/api/clear")
def api_clear(q, b):
    with LOCK: DEV.clear()
    if b.get("db"):
        with db() as c: c.execute("DELETE FROM hits")
    return {"ok": True}

@app.post("/api/test")
def api_test(q, b):
    W.play(SND["near" if b.get("near") else "rf"], CFG["volume"]); return {"ok": True}

def _rows():
    with db() as c: return c.execute("SELECT t,src,addr,name,conf,why,rssi,lat,lon,raw FROM hits ORDER BY t").fetchall()

@app.get("/api/export.csv")
def api_csv(q):
    import csv, io
    s = io.StringIO(); w = csv.writer(s); w.writerow(["time_utc", "source", "address", "name", "confidence", "why", "rssi", "lat", "lon", "raw"])
    for r in _rows(): w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r[0]))] + list(r[1:]))
    return s.getvalue().encode(), "text/csv", {"Content-Disposition": "attachment; filename=alpr-hits.csv"}

@app.get("/api/export.kml")
def api_kml(q):
    from xml.sax.saxutils import escape as X
    col = {"strong": "ff0000ff", "medium": "ff00aaff", "weak": "ff00ffff"}
    pm = []
    for t, src, addr, name, conf, why, rssi, lat, lon, raw in _rows():
        if lat is None: continue
        pm.append("<Placemark><name>%s</name><description>%s</description><Style><IconStyle><color>%s</color></IconStyle></Style>"
                  "<Point><coordinates>%.6f,%.6f</coordinates></Point></Placemark>" % (
                      X(name or addr), X("%s %s %s rssi %s - %s" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(t)), src, addr, rssi, "; ".join(json.loads(why)))),
                      col.get(conf, "ffffffff"), lon, lat))
    k = '<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>ALPR Watch hits</name>%s</Document></kml>' % "".join(pm)
    return k.encode(), "application/vnd.google-earth.kml+xml", {"Content-Disposition": "attachment; filename=alpr-hits.kml"}

@app.get("/leaflet/")
def api_leaflet(q):
    f = os.path.realpath(os.path.join(LEAFLET, q.get("_rest", "")))
    if not f.startswith(os.path.realpath(LEAFLET) + os.sep) or not os.path.isfile(f): return 404, {"ok": False}
    ct = {"js": "application/javascript", "css": "text/css", "png": "image/png", "svg": "image/svg+xml"}.get(f.rsplit(".", 1)[-1], "application/octet-stream")
    with open(f, "rb") as fh: return fh.read(), ct

if __name__ == "__main__":
    W.write_tone(SND["rf"], [(.12, 1800), (.06, 0), (.12, 1800), (.06, 0), (.3, 1200)])
    W.write_tone(SND["near"], [(.18, 880), (.05, 0), (.18, 1320), (.05, 0), (.18, 1760)])
    load_recent(); spawn(W.ble_loop, on_ble); spawn(wifi_loop); spawn(near_loop); app.serve()
