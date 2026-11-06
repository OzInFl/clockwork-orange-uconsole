#!/usr/bin/env python3
"""Clockwork Orange Wireless Census - what's around you on Bluetooth LE and Wi-Fi, and trackers following you (:8096).

Bluetooth: every advertiser, named and classified (Apple Continuity / Find My, Tile, SmartTag, Chipolo, Google Find Hub,
Fast Pair, Eddystone/iBeacon, Xiaomi sensors, Meshtastic...), vendor from the SIG company ID or the MAC's IEEE OUI.
Wi-Fi: access points + IoT SoftAPs with vendor, band/channel, security and the WPS manufacturer/model a lot of IoT
gear announces. Tracker watch (AirGuard-style): a tracker in "separated from owner" mode (or a Tile/SmartTag/Chipolo,
which don't say) seen over >= follow_min minutes at points >= follow_m apart while you moved = "following you" alert.
Passive (BLE scans are BlueZ's normal active scan requests; nothing connects). Data in ~/co-iot/census.db.
"""
import json, os, re, sqlite3, sys, threading, time
sys.path.insert(0, "/opt/clockwork-orange/lib"); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from co_web import App, spawn, load_json, save_json
import co_wireless as W

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.expanduser("~/co-iot"); os.makedirs(DATA, exist_ok=True)
CFGF = os.path.expanduser("~/.config/clockwork-orange/iot.json")
app = App(int(os.environ.get("CO_IOT_PORT") or 8096), os.path.join(HERE, "index.html"))
DEF = {"ble": True, "wifi": True, "wifi_every_s": 30, "keep_h": 12, "follow_min": 10, "follow_m": 500,
       "sound": True, "volume": .8, "mine": []}
CFG = dict(DEF, **load_json(CFGF, {}))
LOCK = threading.Lock()
GPS = W.Gps()
SND = os.path.join(DATA, "sounds", "follow.wav")
BLE, WIFI, DIRTY = {}, {}, set()

# ------------------------------------------------------------------ Wi-Fi classification
WIFI_CATS = [
    ("camera", r"hikvision|dahua|axis comm|wyze|ring\b|arlo|reolink|amcrest|eufy|blink|verkada|hanwha|uniview|lorex|swann|flock|tp-link.*tapo|ezviz|imou"),
    ("smart home", r"espressif|tuya|shelly|sonoff|itead|tasmota|signify|philips light|lifx|kasa|myq|chamberlain|ecobee|rachio|bhyve|orbit irrigation|"
                   r"wemo|belkin|meross|govee|ecovacs|irobot|roborock|switchbot|honeywell|resideo|nest|august|yale|schlage|ezlo|smartthings|wiz\b"),
    ("tv / media", r"roku|sonos|chromecast|fire ?tv|vizio|\[tv\]|\[lg\]|samsung.*tv|bravia|apple tv|denon|bose|yamaha"),
    ("printer", r"^direct-|hp-print|\bhp\b.*(print|laserjet|officejet|envy)|epson|canon|brother|xerox|lexmark"),
    ("vehicle", r"tesla|uconnect|mychevrolet|onstar|ford|toyota|honda|hyundai|kia|subaru|nissan|bmw|audi|volkswagen|rivian|^car|dashcam|viofo|garmin dash"),
    ("hotspot", r"iphone|android|galaxy|pixel|mifi|jetpack|nighthawk m|inseego|franklin|moxee|t-mobile|verizon|at&t|hotspot"),
    ("network", r"netgear|tp-link|linksys|asus|ubiquiti|eero|google wifi|aruba|cisco|meraki|ruckus|mikrotik|gl\.?inet|gl-|arris|technicolor|sagemcom|xfinity|spectrum|calix|eero|orbi|deco"),
]
def wifi_cat(ap, vendor):
    hay = " ".join(x for x in (ap.get("ssid"), vendor, ap["wps"].get("manufacturer"), ap["wps"].get("model"), ap["wps"].get("device name")) if x)
    for c, rx in WIFI_CATS:
        if re.search(rx, hay, re.I): return c
    if ap.get("ssid") == "": return "hidden"
    return "other"

# ------------------------------------------------------------------ handlers
def on_ble(a):
    if not CFG["ble"]: return
    now = time.time(); fx = GPS.fix(); addr = a["addr"]
    with LOCK:
        d = BLE.get(addr)
        if not d: d = BLE[addr] = {"addr": addr, "first": now, "n": 0, "best": -999, "sight": [], "follow": None}
        cl = W.ble_classify(a)
        # BLE random addresses carry no vendor; a public one does (IEEE OUI). Else the SIG company ID of the manufacturer data.
        vendor = (W.oui_vendor(addr) if a["atype"] == "public" else None) or (W.company(next(iter(a["mfr"]))) if a["mfr"] else None)
        d.update(atype=a["atype"], last=now, rssi=a["rssi"], adv=W.adv_hex(a), vendor=vendor or d.get("vendor"),
                 name=a.get("name") or d.get("name"))
        if cl["what"] or not d.get("what"):        # an advert with nothing identifying keeps the last classification
            d.update(cat=cl["cat"] or "unknown", what=cl["what"], tracker=cl["tracker"], separated=cl["separated"])
        d["n"] += 1
        if a["rssi"] is not None and a["rssi"] > d["best"]: d["best"] = a["rssi"]
        if d["tracker"] and fx and (not d["sight"] or now - d["sight"][-1][0] >= 30):
            d["sight"].append((round(now), round(fx["lat"], 6), round(fx["lon"], 6), a["rssi"])); del d["sight"][:-240]
            check_follow(d)
        DIRTY.add(("ble", addr))

def check_follow(d):
    """Following = seen across >= follow_min minutes and the sightings span >= follow_m (you moved and it came along)."""
    s = d["sight"]
    if len(s) < 3: return
    span_min = (s[-1][0] - s[0][0]) / 60
    far = max(W.dist_m(s[0][1], s[0][2], x[1], x[2]) for x in s)
    worry = d["separated"] or d["tracker"] in ("Tile", "SmartTag", "Chipolo")
    d["follow"] = {"min": round(span_min), "m": round(far), "worry": bool(worry)}
    if span_min >= CFG["follow_min"] and far >= CFG["follow_m"] and worry and not d.get("alerted") and d["addr"] not in CFG["mine"]:
        d["alerted"] = time.time()
        if CFG["sound"]: W.play(SND, CFG["volume"])
        app.push({"t": "follow", "d": pub_ble(d)})

def wifi_loop():
    while True:
        if CFG["wifi"]:
            try:
                aps = W.wifi_scan(); now = time.time(); fx = GPS.fix()
                with LOCK:
                    for ap in aps:
                        b = ap["bssid"]; d = WIFI.get(b)
                        if not d: d = WIFI[b] = {"bssid": b, "first": now, "n": 0, "best": -999}
                        v = W.oui_vendor(b)
                        sig = ap.get("signal")
                        d.update(ssid=ap.get("ssid"), vendor=v, chan=ap.get("chan"), freq=ap.get("freq"), sec=ap["sec"], wps=ap["wps"] or d.get("wps") or {},
                                 last=now, signal=sig, assoc=ap["assoc"], rand=W.randomized(b))
                        d["cat"] = wifi_cat(dict(ap, wps=d["wps"]), v); d["n"] += 1
                        if sig is not None and sig > d["best"]:
                            d["best"] = sig
                            if fx: d.update(lat=round(fx["lat"], 6), lon=round(fx["lon"], 6))
                        DIRTY.add(("wifi", b))
            except Exception as e: W.WIFI.update(ok=False, err=str(e))
        time.sleep(max(15, int(CFG["wifi_every_s"])))

# ------------------------------------------------------------------ storage
def db():
    c = sqlite3.connect(os.path.join(DATA, "census.db"), timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS ble (addr TEXT PRIMARY KEY, last REAL, j TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS wifi (bssid TEXT PRIMARY KEY, last REAL, j TEXT)"); return c

def flush_loop():
    while True:
        time.sleep(30)
        with LOCK:
            rows = []
            for k, a in DIRTY:
                d = (BLE if k == "ble" else WIFI).get(a)
                if d: rows.append((k, a, d["last"], json.dumps(d)))
            DIRTY.clear()
            cut = time.time() - CFG["keep_h"] * 3600
            for a in [a for a, d in BLE.items() if d["last"] < cut]: del BLE[a]
            for a in [a for a, d in WIFI.items() if d["last"] < cut]: del WIFI[a]
        with db() as c:
            for k, a, t, j in rows: c.execute("INSERT OR REPLACE INTO %s VALUES (?,?,?)" % k, (a, t, j))

def load_recent():
    cut = time.time() - CFG["keep_h"] * 3600
    with db() as c:
        for a, j in c.execute("SELECT addr, j FROM ble WHERE last > ?", (cut,)): BLE[a] = json.loads(j)
        for b, j in c.execute("SELECT bssid, j FROM wifi WHERE last > ?", (cut,)): WIFI[b] = json.loads(j)
    for d in BLE.values(): d["sight"] = [tuple(x) for x in d.get("sight", [])]

# ------------------------------------------------------------------ API
def pub_ble(d): return {k: v for k, v in d.items() if k != "sight"} | {"sights": len(d.get("sight", []))}

def summary():
    now = time.time()
    with LOCK:
        live_b = [d for d in BLE.values() if now - d["last"] < 120]; live_w = [d for d in WIFI.values() if now - d["last"] < 180]
        cats = {}
        for d in live_b: cats.setdefault(d["cat"], [0, 0])[0] += 1
        for d in live_w: cats.setdefault(d["cat"], [0, 0])[1] += 1
        trk = [pub_ble(d) for d in BLE.values() if d.get("tracker")]
    return {"ble_live": len(live_b), "ble_total": len(BLE), "wifi_live": len(live_w), "wifi_total": len(WIFI), "cats": cats,
            "trackers": len([t for t in trk if now - t["last"] < 300]), "following": len([t for t in trk if t.get("alerted")]),
            "ble": {k: W.BLE[k] for k in ("ok", "err", "t", "n")}, "wifi": {k: W.WIFI[k] for k in ("ok", "err", "t", "iface")},
            "gps": GPS.fix(), "now": now}

@app.get("/api/state")
def api_state(q): return {"cfg": CFG, "sum": summary()}

@app.get("/api/ble")
def api_ble(q):
    with LOCK: return sorted((pub_ble(d) for d in BLE.values()), key=lambda d: -d["last"])

@app.get("/api/wifi")
def api_wifi(q):
    with LOCK: return sorted(WIFI.values(), key=lambda d: -d["last"])

@app.get("/api/trackers")
def api_trk(q):
    with LOCK: return sorted((dict(d, mine=d["addr"] in CFG["mine"]) for d in BLE.values() if d.get("tracker")), key=lambda d: -d["last"])

@app.post("/api/mine")
def api_mine(q, b):
    a = b.get("addr", "").upper()
    CFG["mine"] = [x for x in CFG["mine"] if x != a] + ([a] if b.get("on") else []); save_json(CFGF, CFG)
    return {"ok": True}

@app.post("/api/cfg")
def api_cfg(q, b):
    for k, v in b.items():
        if k in DEF and k != "mine": CFG[k] = type(DEF[k])(v)
    save_json(CFGF, CFG); return {"ok": True, "cfg": CFG}

@app.post("/api/clear")
def api_clear(q, b):
    with LOCK: BLE.clear(); WIFI.clear(); DIRTY.clear()
    with db() as c: c.execute("DELETE FROM ble"); c.execute("DELETE FROM wifi")
    return {"ok": True}

@app.post("/api/test")
def api_test(q, b): W.play(SND, CFG["volume"]); return {"ok": True}

@app.get("/api/export.csv")
def api_csv(q):
    import csv, io
    s = io.StringIO(); w = csv.writer(s)
    w.writerow(["type", "address", "name_or_ssid", "vendor", "category", "what", "first_utc", "last_utc", "seen", "best_dbm", "channel", "security", "wps", "lat", "lon"])
    T = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
    with LOCK:
        for d in BLE.values(): w.writerow(["ble", d["addr"], d.get("name"), d.get("vendor"), d.get("cat"), d.get("what"), T(d["first"]), T(d["last"]), d["n"], d["best"], "", "", "", "", ""])
        for d in WIFI.values(): w.writerow(["wifi", d["bssid"], d.get("ssid"), d.get("vendor"), d.get("cat"), "", T(d["first"]), T(d["last"]), d["n"], d["best"],
                                            d.get("chan"), d.get("sec"), " ".join("%s=%s" % kv for kv in (d.get("wps") or {}).items()), d.get("lat"), d.get("lon")])
    return s.getvalue().encode(), "text/csv", {"Content-Disposition": "attachment; filename=wireless-census.csv"}

if __name__ == "__main__":
    W.write_tone(SND, [(.25, 660), (.08, 0), (.25, 660), (.08, 0), (.25, 660), (.3, 0), (.5, 440)])
    load_recent(); spawn(W.ble_loop, on_ble); spawn(wifi_loop); spawn(flush_loop); app.serve()
