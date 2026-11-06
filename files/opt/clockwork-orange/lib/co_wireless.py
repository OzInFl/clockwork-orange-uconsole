"""co_wireless - shared passive Wi-Fi + BLE scanning for ALPR Watch and Wireless Census (also gpsd, vendors, alert sounds).

BLE: bleak on BlueZ (python3-bleak). ble_loop(cb) runs a scanner forever in its own thread; several apps can scan at
once (BlueZ merges discovery sessions). cb gets {addr, atype, name, rssi, tx, mfr{cid:bytes}, uuids[], sdata{uuid:bytes}}.
Wi-Fi: the built-in radio has no monitor mode, so this sees access points/SoftAPs (beacons + probe responses), not
client probe requests. wifi_scan() asks NetworkManager for a fresh scan (polkit rule from step 90 allows netdev
members) and reads the full results with `iw scan dump` (WPS manufacturer/model, security, vendor IEs).
Vendors: IEEE OUI registry (ieee-data) for MACs, Bluetooth SIG company IDs + 16-bit UUID names from bleak's tables.
"""
import csv, io, math, os, re, socket, struct, subprocess, threading, time, wave, json

IW = "/usr/sbin/iw" if os.path.exists("/usr/sbin/iw") else "iw"

def spawn(f, *a):
    t = threading.Thread(target=f, args=a, daemon=True); t.start(); return t

# ------------------------------------------------------------------ vendors
_OUI = {}
_OUI_LOCK = threading.Lock()
def _load_oui():
    with _OUI_LOCK:
        if _OUI: return
        for fn, n in (("oui.csv", 6), ("mam.csv", 7), ("oui36.csv", 9)):
            try:
                with open("/usr/share/ieee-data/" + fn, newline="", encoding="utf-8", errors="replace") as f:
                    for r in csv.reader(f):
                        if len(r) > 2 and len(r[1]) == n: _OUI[r[1].upper()] = r[2].strip()
            except OSError: pass
        _OUI["_"] = "loaded"

def randomized(mac):
    """Locally administered bit set = randomized / private address (no vendor to look up)."""
    try: return bool(int(mac[1], 16) & 2)
    except (ValueError, IndexError): return False

def oui_vendor(mac):
    if not mac or randomized(mac): return None
    _load_oui(); h = mac.replace(":", "").replace("-", "").upper()
    return _OUI.get(h[:9]) or _OUI.get(h[:7]) or _OUI.get(h[:6])

try:
    from bleak.backends._manufacturers import MANUFACTURERS as _CID
except Exception: _CID = {}
try:
    from bleak.uuids import uuid16_dict as _U16
except Exception: _U16 = {}

def company(cid): return _CID.get(cid)

def short_uuid(u):
    """'0000feed-0000-1000-8000-00805f9b34fb' -> 0xFEED (16-bit SIG UUID) else None."""
    u = u.lower()
    if u.endswith("-0000-1000-8000-00805f9b34fb") and u.startswith("0000"): return int(u[4:8], 16)
    return None

def uuid_name(u):
    s = short_uuid(u)
    return (_U16.get(s) or "0x%04X" % s) if s is not None else u

# ------------------------------------------------------------------ BLE classification (Wireless Census + tracker watch)
APPLE_TYPES = {0x02: "iBeacon", 0x05: "AirDrop", 0x07: "AirPods / Beats", 0x09: "AirPlay target", 0x0A: "AirPlay source",
               0x0C: "Handoff", 0x0D: "Wi-Fi hotspot", 0x0E: "Instant hotspot", 0x0F: "Nearby action", 0x10: "Nearby (iPhone/iPad/Mac/Watch)",
               0x12: "Find My", 0x16: "Nearby info"}
FINDMY_KIND = {0: "Apple device", 1: "AirTag", 2: "Find My accessory", 3: "AirPods"}   # status byte bits 4-5 (AirGuard)
CAT_WORDS = [("audio", r"airpods|buds|jbl|bose|beats|sony wh|wf-|soundcore|headphone|speaker|partybox|sonos"),
             ("wearable", r"watch|band|fitbit|garmin|whoop|oura|ring\b|smartring|amazfit|mi band"),
             ("tv / media", r"\btv\b|webos|roku|chromecast|fire ?tv|bravia|vizio|\[tv\]"),
             ("smart home", r"govee|ihoment|bhyve|hue|wyze|ring|nest|tuya|switchbot|yeelight|lock|thermostat|kasa|meross|swei"),
             ("vehicle", r"tesla|ford|toyota|honda|\bcar\b|obd|uconnect|mychevrolet|bmw|subaru"),
             ("phone / computer", r"iphone|ipad|macbook|pixel|galaxy|android|desktop|laptop|surface")]

def ble_classify(a):
    """-> {cat, what, tracker (None | 'Find My' / 'Tile' / 'SmartTag' / 'Chipolo' / 'Google Find Hub'), separated, detail}"""
    m, sd = a.get("mfr") or {}, a.get("sdata") or {}
    s16 = {short_uuid(u) for u in (a.get("uuids") or [])} | {short_uuid(u) for u in sd}
    name = a.get("name") or ""
    r = {"cat": None, "what": None, "tracker": None, "separated": False, "detail": None}
    ap = m.get(0x004C)
    if ap and len(ap) >= 2:
        t = ap[0]; r["what"] = "Apple " + APPLE_TYPES.get(t, "0x%02X" % t)
        if t == 0x12:
            # iPhones/Macs/AirPods send Find My frames too: only AirTags, third-party Find My tags, or anything
            # separated from its owner count as trackers.
            st = ap[2] if len(ap) > 2 else 0; kind = (st >> 4) & 3; sep = ap[1] >= 0x19
            r.update(what=FINDMY_KIND[kind] + " (Find My" + (", separated from owner)" if sep else ", near owner)"), separated=sep,
                     cat="tracker" if kind in (1, 2) or sep else "audio" if kind == 3 else "phone / computer")
            if r["cat"] == "tracker": r["tracker"] = "Find My"
        elif t in (0x07,): r["cat"] = "audio"
        elif t == 0x02: r["cat"] = "beacon"
        else: r["cat"] = "phone / computer"
    if 0xFEED in s16 or 0xFEEC in s16 or 0xFEED in m: r.update(tracker="Tile", cat="tracker", what="Tile tracker")
    if 0xFD5A in s16: r.update(tracker="SmartTag", cat="tracker", what="Samsung SmartTag")
    if 0xFE33 in s16 or 0xFE65 in s16: r.update(tracker="Chipolo", cat="tracker", what="Chipolo tracker")
    ed = next((v for u, v in sd.items() if short_uuid(u) == 0xFEAA), None)
    if ed:
        if ed[:1] in (b"\x40", b"\x41"): r.update(tracker="Google Find Hub", cat="tracker", what="Google Find Hub tag",
                                                  separated=ed[:1] == b"\x41")
        elif not r["what"]: r.update(cat="beacon", what="Eddystone beacon")
    if 0xFD6F in s16 and not r["what"]: r.update(cat="phone / computer", what="Exposure Notification (phone)")
    if 0xFE2C in s16 and not r["what"]: r.update(cat="audio", what="Google Fast Pair device")
    if 0xFE95 in s16 and not r["what"]: r.update(cat="smart home", what="Xiaomi MiBeacon sensor")
    if any(u.lower().startswith("6ba1b218") for u in a.get("uuids") or []): r.update(cat="radio", what="Meshtastic node")
    if 0x0006 in m and not r["what"]: r.update(cat="phone / computer", what="Windows PC / Microsoft device")
    if not r["cat"] and name:
        for c, rx in CAT_WORDS:
            if re.search(rx, name, re.I): r["cat"] = c; break
    if not r["what"]:
        cid = next(iter(m), None)
        r["what"] = (company(cid) if cid is not None else None) or next((uuid_name(u) for u in (a.get("uuids") or [])), None)
    return r

# ------------------------------------------------------------------ BLE scanner
BLE = {"ok": False, "err": None, "t": 0, "n": 0}
def ble_loop(cb):
    """Run forever: BleakScanner callback -> cb(adv). Restarts after adapter errors (BT off, bluetoothd restart)."""
    import asyncio
    from bleak import BleakScanner
    def conv(d, a):
        p = d.details.get("props", {}) if isinstance(d.details, dict) else {}
        BLE["t"] = time.time(); BLE["n"] += 1
        try:
            cb({"addr": d.address.upper(), "atype": p.get("AddressType") or "?", "name": a.local_name or p.get("Name"),
                "rssi": a.rssi, "tx": a.tx_power, "mfr": {k: bytes(v) for k, v in a.manufacturer_data.items()},
                "uuids": [u.lower() for u in a.service_uuids], "sdata": {k.lower(): bytes(v) for k, v in a.service_data.items()}})
        except Exception as e: BLE["err"] = "handler: %s" % e
    async def run():
        async with BleakScanner(conv):
            BLE.update(ok=True, err=None)
            while True: await asyncio.sleep(5)
    while True:
        try: asyncio.run(run())
        except Exception as e: BLE.update(ok=False, err="%s: %s" % (type(e).__name__, e))
        time.sleep(10)

def adv_hex(a):
    """Compact text of an advertisement for logs/UI: mfr 0x004C:1219..., svc FEED, data FD5A:..."""
    p = ["mfr %04X:%s" % (k, v.hex()) for k, v in (a.get("mfr") or {}).items()]
    p += ["svc " + ("%04X" % short_uuid(u) if short_uuid(u) is not None else u) for u in a.get("uuids") or []]
    p += ["data %s:%s" % ("%04X" % short_uuid(u) if short_uuid(u) is not None else u, v.hex()) for u, v in (a.get("sdata") or {}).items()]
    return " ".join(p)

# ------------------------------------------------------------------ Wi-Fi scanner
def wifi_iface():
    for n in sorted(os.listdir("/sys/class/net")):
        if os.path.isdir("/sys/class/net/%s/wireless" % n): return n
    return None

def freq_chan(f):
    f = int(f)
    if f == 2484: return 14
    if 2412 <= f < 2484: return (f - 2407) // 5
    if 5000 <= f < 5925: return (f - 5000) // 5
    if f >= 5925: return (f - 5950) // 5
    return None

def parse_iw(txt):
    aps, cur, sect = [], None, None
    for ln in txt.splitlines():
        m = re.match(r"^BSS ([0-9a-f:]{17})", ln)
        if m:
            cur = {"bssid": m.group(1).upper(), "ssid": None, "sec": "open", "wps": {}, "assoc": "associated" in ln, "vie": []}
            aps.append(cur); sect = None; continue
        if not cur: continue
        s = ln.strip()
        if ln.startswith("\t") and not ln.startswith("\t\t") and not s.startswith("*"): sect = s.split(":")[0]
        if s.startswith("SSID:"): cur["ssid"] = s[5:].strip()
        elif s.startswith("freq:"): cur["freq"] = int(float(s[5:])); cur["chan"] = freq_chan(cur["freq"])
        elif s.startswith("signal:"): cur["signal"] = float(s.split()[1])
        elif s.startswith("last seen:") and "ago" in s: cur["age_ms"] = int(s.split()[2])
        elif s.startswith("capability:") and "Privacy" in s and cur["sec"] == "open": cur["sec"] = "WEP"
        elif s.startswith("RSN:"): cur["sec"] = "WPA2"
        elif s.startswith("WPA:") and cur["sec"] != "WPA2": cur["sec"] = "WPA"
        elif sect == "RSN" and "SAE" in s: cur["sec"] = "WPA3"
        elif sect == "WPS" and s.startswith("*"):
            k, _, v = s[1:].partition(":"); k = k.strip().lower()
            if k in ("manufacturer", "model", "model number", "device name", "serial number") and v.strip(): cur["wps"][k] = v.strip()
        elif s.startswith("Vendor specific: OUI"):
            o = s.split("OUI")[1].split(",")[0].strip().upper()
            if o not in cur["vie"]: cur["vie"].append(o)
    for a in aps:
        if a["ssid"] is not None and (a["ssid"] == "" or set(a["ssid"]) <= {"\\", "x", "0"}): a["ssid"] = ""   # hidden
    return aps

WIFI = {"ok": False, "err": None, "t": 0, "iface": None}
def wifi_scan(max_age_s=90):
    """Trigger a scan (best effort), wait for it, return fresh APs."""
    i = WIFI["iface"] = WIFI["iface"] or wifi_iface()
    if not i: WIFI.update(ok=False, err="no Wi-Fi interface"); return []
    r = subprocess.run(["nmcli", "dev", "wifi", "rescan", "ifname", i], capture_output=True, text=True, timeout=20)
    trig = r.returncode == 0 or "not allowed immediately" in r.stderr
    time.sleep(5)
    out = subprocess.run([IW, "dev", i, "scan", "dump"], capture_output=True, text=True, timeout=20)
    if out.returncode: WIFI.update(ok=False, err=(out.stderr or "iw failed").strip()); return []
    aps = [a for a in parse_iw(out.stdout) if a.get("age_ms", 0) <= max_age_s * 1000]
    WIFI.update(ok=True, t=time.time(), err=None if trig else "can't trigger scans (%s); showing cached results" % (r.stderr.strip() or "nmcli"))
    return aps

# ------------------------------------------------------------------ GPS (gpsd)
class Gps:
    def __init__(self): self.f = {}; spawn(self._loop)
    def _loop(self):
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
                        except ValueError: continue
                        if m.get("class") == "TPV" and m.get("mode", 0) >= 2 and m.get("lat") is not None:
                            self.f = {"lat": m["lat"], "lon": m["lon"], "speed": m.get("speed"), "track": m.get("track"), "t": time.time()}
            except Exception: pass
            time.sleep(5)
    def fix(self, max_age=10):
        f = self.f
        return f if f and time.time() - f["t"] < max_age else None

def dist_m(la1, lo1, la2, lo2):
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    return 2 * 6371000.0 * math.asin(math.sqrt(math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))

def bearing(la1, lo1, la2, lo2):
    p1, p2, dl = math.radians(la1), math.radians(la2), math.radians(lo2 - lo1)
    return (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360

# ------------------------------------------------------------------ alert sounds (uConsole speaker)
def write_tone(path, parts, fs=22050):
    """parts: [(seconds, freq_hz or 0)] -> 16-bit mono WAV with short fades."""
    out = []
    for secs, f in parts:
        n = int(secs * fs); fade = int(.005 * fs)
        for i in range(n):
            g = min(1.0, i / fade, (n - 1 - i) / fade)
            out.append(int(math.sin(2 * math.pi * f * i / fs) * g * 24000) if f else 0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(fs); w.writeframes(struct.pack("<%dh" % len(out), *out))

_PLAY = threading.Lock()
def play(path, vol=.8):
    if not _PLAY.acquire(blocking=False): return
    def run():
        try:
            env = dict(os.environ, XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid())
            r = subprocess.run(["pw-play", "--volume", str(vol), path], env=env, capture_output=True, timeout=15)
            if r.returncode: subprocess.run(["aplay", "-q", path], env=env, capture_output=True, timeout=15)
        except Exception: pass
        finally: _PLAY.release()
    spawn(run)
