#!/usr/bin/env python3
"""Clockwork Orange GPS Status - graphical gpsd view for the AIO V2 GPS (:8093).

One gpsd watch with JSON + NMEA: TPV (fix/position/speed/time), SKY (satellites, DOP) and the module's own
$GPTXT lines (ANTENNA OPEN/OK/SHORT, which gpsd's JSON doesn't carry). Pushed to the page over SSE once a second.
Also tracks time-to-first-fix and a short position trail. Receive only, stdlib only.
"""
import json, os, socket, subprocess, sys, threading, time
sys.path.insert(0, "/opt/clockwork-orange/lib"); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from co_web import App, spawn

HERE = os.path.dirname(os.path.abspath(__file__))
app = App(int(os.environ.get("CO_GPS_PORT") or 8093), os.path.join(HERE, "index.html"))
LOCK = threading.Lock()
G = {"gpsd": False, "device": None, "tpv": {}, "sky": {}, "sats": [], "antenna": None, "antenna_t": None, "txt": [],
     "nmea_t": None, "started": time.time(), "ttff": None, "nofix_since": time.time(), "trail": [], "err": None}

def _nmea(line):
    t = line.split("*")[0]
    if t[3:6] == "TXT":
        msg = t.split(",", 4)[-1].strip()
        with LOCK:
            if "ANTENNA" in msg.upper(): G["antenna"] = msg.upper().replace("ANTENNA", "").strip() or msg; G["antenna_t"] = time.time()
            elif msg not in G["txt"]: G["txt"] = (G["txt"] + [msg])[-6:]

def _json(m):
    c = m.get("class")
    with LOCK:
        if c == "DEVICES" and m.get("devices"):
            d = m["devices"][0]; G["device"] = {k: d.get(k) for k in ("path", "driver", "subtype", "bps")}
        elif c == "DEVICE":
            G["device"] = {k: m.get(k, (G["device"] or {}).get(k)) for k in ("path", "driver", "subtype", "bps")}
        elif c == "TPV":
            G["tpv"] = {k: m.get(k) for k in ("mode", "time", "lat", "lon", "altMSL", "altHAE", "alt", "speed", "track", "climb",
                                             "eph", "epv", "ept", "eps", "epx", "epy", "magvar", "leapseconds", "status")}
            mode = m.get("mode") or 0
            if mode >= 2:
                if G["ttff"] is None and G["nofix_since"]: G["ttff"] = round(time.time() - G["nofix_since"])
                G["nofix_since"] = None
                if m.get("lat") is not None:
                    tr = G["trail"]
                    if not tr or abs(tr[-1][0] - m["lat"]) + abs(tr[-1][1] - m["lon"]) > 2e-6:
                        tr.append((m["lat"], m["lon"], time.time())); del tr[:-600]
            elif G["nofix_since"] is None:
                G["nofix_since"] = time.time(); G["ttff"] = None
        elif c == "SKY":
            G["sky"] = {k: m.get(k) for k in ("hdop", "vdop", "pdop", "gdop", "tdop", "nSat", "uSat")}
            if "satellites" in m:
                G["sats"] = [{k: s.get(k) for k in ("PRN", "gnssid", "svid", "el", "az", "ss", "used", "health")} for s in m["satellites"]]

def reader():
    while True:
        try:
            s = socket.create_connection(("127.0.0.1", 2947), timeout=5)
            s.sendall(b'?WATCH={"enable":true,"json":true,"nmea":true}\n'); s.settimeout(5)
            with LOCK: G.update(gpsd=True, err=None)
            buf = b""
            while True:
                try: chunk = s.recv(8192)
                except socket.timeout:
                    with LOCK: G["err"] = "gpsd connected but the GPS is silent (rail off? co-rail gps on)"
                    continue
                if not chunk: break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    ln = ln.decode(errors="replace").strip()
                    if ln.startswith("$"):
                        with LOCK: G["nmea_t"] = time.time(); G["err"] = None
                        _nmea(ln)
                    elif ln.startswith("{"):
                        try: _json(json.loads(ln))
                        except ValueError: pass
        except Exception as e:
            with LOCK: G.update(gpsd=False, err="gpsd not reachable: %s" % e)
        time.sleep(3)

RAIL_T = {"t": 0, "v": None}
def snapshot():
    with LOCK:
        d = {k: G[k] for k in ("gpsd", "device", "tpv", "sky", "sats", "antenna", "antenna_t", "txt", "nmea_t", "ttff", "nofix_since", "err", "started")}
        d["trail"] = G["trail"][-300:]
    d["now"] = time.time()
    if time.time() - RAIL_T["t"] > 5:                       # co-rail is a subprocess: check it at most every 5 s
        try: RAIL_T["v"] = subprocess.run(["co-rail", "gps", "status"], capture_output=True, text=True, timeout=3).stdout.strip().endswith(" on")
        except Exception: RAIL_T["v"] = None
        RAIL_T["t"] = time.time()
    d["rail"] = RAIL_T["v"]
    return d
def ticker():
    while True:
        time.sleep(1)
        if app.clients():
            d = snapshot(); app.push({"t": "gps", "d": d})

@app.get("/api/state")
def api_state(q): return snapshot()

@app.post("/api/trail/clear")
def api_tclear(q, b):
    with LOCK: G["trail"] = []
    return {"ok": True}

@app.post("/api/rail")
def api_rail(q, b):
    on = "on" if b.get("on") else "off"
    subprocess.run(["co-rail", "gps", on], capture_output=True, timeout=10); RAIL_T["t"] = 0
    return {"ok": True, "rail": on}

if __name__ == "__main__":
    spawn(reader); spawn(ticker); app.serve()
