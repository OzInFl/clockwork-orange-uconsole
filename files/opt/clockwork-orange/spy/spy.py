#!/usr/bin/env python3
"""Clockwork Orange System Spy - find signals, say what they are, and decode the ones we have decoders for.

Sweep:     rtl_power over the enabled bands -> carriers above the noise floor (per band median + threshold).
Identify:  US band plan (IDENT) by frequency + bandwidth; "probe" runs multimon-ng with every paging/data demod
           on a carrier, so whatever decodes names the signal for real.
Decoders:  multimon-ng (POCSAG/FLEX pagers, APRS AFSK1200, NOAA SAME/EAS), rtl_433 (ISM/IoT sensors, ~250 device
           types), readsb (ADS-B aircraft). P25/DMR is handed to Looking Glass.
2.4 GHz:   nRF52840 USB dongle with Nordic's nRF 802.15.4 sniffer firmware: channel survey 11-26 and live monitor,
           frames parsed to Zigbee / Thread networks (co_decode.parse_154).
One RTL job at a time (co_radio lease: second USB RTL first, else the AIO one borrowed from Looking Glass).
Web UI + API on :8092. Receive only.
"""
import glob, json, os, queue, re, shutil, socket, statistics, subprocess, sys, tempfile, threading, time
sys.path.insert(0, "/opt/clockwork-orange/lib"); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import co_radio, co_decode
from co_web import App, spawn, load_json, save_json

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CO_SPY_PORT") or 8092)
CFG_PATH = os.path.expanduser("~/.config/clockwork-orange/spy.json")
LOGDIR = os.path.expanduser("~/co-spy")
app = App(PORT, os.path.join(HERE, "index.html"))
LOCK = threading.RLock()

# ------------------------------------------------------------------ bands to sweep (MHz, bin Hz)
BANDS = [
    {"id": "air",   "name": "Airband (AM)",               "lo": 118.0,  "hi": 137.0,  "bin": 12500, "on": False},
    {"id": "2m",    "name": "2 m amateur / APRS",          "lo": 144.0,  "hi": 148.0,  "bin": 5000,  "on": True},
    {"id": "vhf",   "name": "VHF high (pagers, marine, NOAA, MURS, business)", "lo": 150.0, "hi": 174.0, "bin": 6250, "on": True},
    {"id": "ism315","name": "315 MHz ISM (car fobs, TPMS, sensors)", "lo": 314.0, "hi": 316.0, "bin": 10000, "on": True},
    {"id": "sec",   "name": "319-345 MHz security sensors", "lo": 318.5, "hi": 346.0, "bin": 25000, "on": False},
    {"id": "70cm",  "name": "70 cm amateur + 433 ISM",      "lo": 420.0,  "hi": 450.0,  "bin": 12500, "on": True},
    {"id": "uhf",   "name": "UHF business / FRS / GMRS",    "lo": 450.0,  "hi": 470.0,  "bin": 6250,  "on": True},
    {"id": "700",   "name": "700 MHz public safety",        "lo": 769.0,  "hi": 775.0,  "bin": 6250,  "on": True},
    {"id": "800",   "name": "800 MHz trunking",             "lo": 851.0,  "hi": 869.0,  "bin": 6250,  "on": True},
    {"id": "900",   "name": "900 MHz ISM (LoRa, Z-Wave, meters)", "lo": 902.0, "hi": 928.0, "bin": 25000, "on": True},
    {"id": "page9", "name": "929-932 MHz paging",           "lo": 929.0,  "hi": 932.0,  "bin": 6250,  "on": True},
    {"id": "adsb",  "name": "1090 MHz ADS-B",               "lo": 1086.0, "hi": 1094.0, "bin": 50000, "on": False},
    {"id": "fm",    "name": "FM broadcast",                 "lo": 88.0,   "hi": 108.0,  "bin": 50000, "on": False},
]

# ------------------------------------------------------------------ identification (US band plan)
# (lo MHz, hi MHz, label, family, decoder or None, note). First match wins, so specific entries come first.
def _pt(f, w=0.006): return (f - w, f + w)
IDENT = [
    (*_pt(144.390, .008), "APRS", "data", "aprs", "Amateur packet: positions, weather, messages (AFSK 1200)"),
    (*_pt(161.975, .010), "AIS channel A", "data", "ais", "Ship positions, names, destinations (GMSK 9600, AIS-catcher)"),
    (*_pt(162.025, .010), "AIS channel B", "data", "ais", "Ship positions, names, destinations (GMSK 9600, AIS-catcher)"),
    (162.390, 162.560, "NOAA Weather Radio", "broadcast", "eas", "Voice forecasts; SAME alert headers decode with multimon-ng EAS"),
    (*_pt(908.42, .06), "Z-Wave (US)", "iot", None, "Home automation mesh, 908.42 MHz. Identified by frequency; no RTL Z-Wave decoder."),
    (*_pt(916.0, .06), "Z-Wave LR / 916 (US)", "iot", None, "Z-Wave 916 MHz channel. Identified by frequency."),
    (136.6, 137.0, "VDL2 aircraft datalink", "data", "vdl2", "Digital ACARS successor 136.65-136.975 MHz (dumpvdl2)"),
    (129.0, 131.8, "ACARS / airband data", "data", "acars", "Aircraft datalink 129.125-131.55 MHz (acarsdec + libacars)"),
    (910.0, 920.0, "Utility meters (ERT / R900)", "iot", "meters", "Electric/gas/water meter readings hopping 910-920 MHz (rtl_433 ERT + Neptune decoders)"),
    (118.0, 137.0, "Airband voice (AM)", "voice", None, "Aviation tower/approach/ATIS"),
    (*_pt(151.820), "MURS", "voice", None, "Multi-Use Radio Service (license-free FM)"), (*_pt(151.880), "MURS", "voice", None, ""),
    (*_pt(151.940), "MURS", "voice", None, ""), (*_pt(154.570), "MURS", "voice", None, ""), (*_pt(154.600), "MURS", "voice", None, ""),
    (156.000, 157.450, "Marine VHF voice", "voice", None, "Ship / coast stations (ch 16 = 156.800 distress)"),
    (152.000, 152.250, "VHF paging", "paging", "pager", "Common POCSAG/FLEX paging allocations"),
    (157.700, 158.750, "VHF paging", "paging", "pager", "Paging / business; POCSAG/FLEX likely if it chirps"),
    (144.0, 148.0, "2 m amateur", "voice", "probe", "Ham FM voice, repeaters, packet"),
    (150.0, 174.0, "VHF business / public safety", "voice", "probe", "FM voice, P25/DMR or data. Probe tries the data decoders"),
    (314.5, 315.5, "315 MHz ISM devices", "iot", "rtl433", "Car key fobs, TPMS, garage doors, sensors (OOK/FSK bursts)"),
    (318.5, 320.0, "319.5 MHz security sensors", "iot", "rtl433", "Interlogix/GE alarm sensors"),
    (344.5, 345.5, "345 MHz security sensors", "iot", "rtl433", "Honeywell 5800 alarm sensors"),
    (433.05, 434.79, "433 MHz ISM devices", "iot", "rtl433", "Weather stations, doorbells, remotes, sensors"),
    (420.0, 450.0, "70 cm amateur", "voice", "probe", "Ham FM/DMR/digital voice, repeaters"),
    (462.540, 462.735, "FRS / GMRS", "voice", None, "Family radio / GMRS simplex + repeater outputs"),
    (467.540, 467.735, "FRS / GMRS (repeater inputs)", "voice", None, ""),
    (450.0, 470.0, "UHF business / public safety", "voice", "probe", "FM voice, P25/DMR or data"),
    (769.0, 775.0, "700 MHz public safety", "trunking", "lg", "P25 trunking: open in Looking Glass / CC Viewer"),
    (799.0, 805.0, "700 MHz public safety (mobile)", "trunking", "lg", ""),
    (851.0, 869.0, "800 MHz trunking", "trunking", "lg", "P25/SmartNet trunking: open in Looking Glass / CC Viewer"),
    (902.0, 928.0, "900 MHz ISM", "iot", "rtl433", "Meters, sensors, LoRa (wide carriers), Z-Wave. rtl_433 decodes many FSK devices here"),
    (929.0, 932.0, "929-932 MHz paging", "paging", "pager", "POCSAG/FLEX commercial and hospital paging"),
    (1088.0, 1092.0, "ADS-B / Mode S", "data", "adsb", "Aircraft transponders (1090 MHz)"),
    (88.0, 108.0, "FM broadcast", "broadcast", None, "Wide FM stations (RDS data not decoded here)"),
]
DECODERS = {"pager": "Pager (POCSAG/FLEX)", "aprs": "APRS (AFSK 1200)", "eas": "NOAA SAME / EAS", "probe": "Probe (all data decoders)",
            "rtl433": "ISM / IoT sensors (rtl_433)", "adsb": "ADS-B aircraft (readsb)", "lg": "Looking Glass (P25/DMR)",
            "meters": "Utility meters (ERT / R900)", "ais": "AIS ships (AIS-catcher)", "acars": "ACARS aircraft datalink (acarsdec)", "vdl2": "VDL2 aircraft datalink (dumpvdl2)", "wxsat": "Weather satellite (SatDump, Meteor-M LRPT)",
            "dsd": "Digital voice (dsd-fme: DMR / P25 / NXDN / YSF / D-STAR / dPMR)"}

def identify(mhz, bw_khz=None):
    for lo, hi, label, fam, dec, note in IDENT:
        if lo <= mhz <= hi:
            if label in ("900 MHz ISM", "Utility meters (ERT / R900)") and bw_khz and bw_khz >= 100:
                return {"label": "LoRa / wideband ISM", "family": "iot", "decoder": None,
                        "note": "Wide (%.0f kHz) 900 MHz carrier: LoRa/Meshtastic or frequency-hopping data. Use the Meshtastic / MeshCore apps (SX1262)." % bw_khz}
            return {"label": label, "family": fam, "decoder": dec, "note": note}
    return {"label": "Unknown carrier", "family": "unknown", "decoder": "probe", "note": "Not in the band table. Probe tries the data decoders."}

# ------------------------------------------------------------------ state
CFG = dict({"thr_db": 10.0, "hold_s": 10, "gain": "40", "ppm": None, "device": None, "decode_secs": 30, "dwell_154": 3,
            "bands": {b["id"]: b["on"] for b in BANDS}}, **load_json(CFG_PATH, {}))
SIG = {}            # key (kHz rounded to 6.25) -> signal record
CAPS = []           # decode sessions, newest last
JOB = {"cur": None, "q": [], "cancel": False, "proc": None, "log": []}
JOBQ = queue.Queue()
SWEEP = {"bands": {}, "last": None}   # band id -> {"f": [...MHz], "db": [...], "floor", "t"}
NRF = {"port": None, "state": "idle", "chan": None, "survey": {}, "nets": {}, "frames": [], "count": 0, "err": None, "stop": False}

def save_cfg(): save_json(CFG_PATH, CFG)

def log(msg):
    with LOCK:
        JOB["log"].append({"t": time.time(), "msg": msg}); del JOB["log"][:-80]
    app.push({"t": "log", "msg": msg})

def ppm_for(lease):
    if CFG.get("ppm") not in (None, ""): return int(round(float(CFG["ppm"])))
    if lease.dev["aio"]:
        try: return int(round(float(re.search(r"^SDR_PPM=([-\d.]+)", open("/etc/default/sdr-looking-glass").read(), re.M).group(1))))
        except Exception: pass
    return 0

def _gain_args(flag="-g"):
    g = str(CFG.get("gain", "")).strip().lower()
    return [] if g in ("", "auto") else [flag, g]

def _proc(argv, **kw):
    kw.setdefault("stderr", subprocess.PIPE)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, **kw)
    with LOCK: JOB["proc"] = p
    return p

def _stop_proc():
    p = JOB.get("proc")
    if p and p.poll() is None:
        try: p.terminate()
        except Exception: pass

# ------------------------------------------------------------------ sweep
def sweep_band(lease, b):
    """rtl_power over a band for hold_s seconds, 1 s passes, max-hold per bin -> list of (MHz, dB).
    Max-hold catches bursty signals (pagers, ISM sensors) that a single averaged pass misses."""
    hold = max(1, int(float(CFG.get("hold_s") or 10)))
    argv = ["rtl_power", "-d", str(lease.index), "-f", "%dk:%dk:%d" % (b["lo"] * 1000, b["hi"] * 1000, b["bin"]),
            "-i", "1", "-c", "0.2", "-p", str(ppm_for(lease))] + (["-1"] if hold <= 1 else ["-e", "%ds" % hold]) + _gain_args() + ["-"]
    p = _proc(argv); mx = {}
    try: out, _ = p.communicate(timeout=hold + max(20, (b["hi"] - b["lo"]) * 2.5))
    except subprocess.TimeoutExpired:
        p.kill(); out = p.stdout.read() if p.stdout else b""
    for ln in out.decode(errors="replace").splitlines():
        x = [s.strip() for s in ln.split(",")]
        if len(x) < 7: continue
        try:
            f0, step = float(x[2]), float(x[4])
            for i, v in enumerate(x[6:]):
                if v not in ("nan", "-nan", ""):
                    f = round((f0 + (i + .5) * step) / 1e6, 6); v = float(v)
                    if v > mx.get(f, -999): mx[f] = v
        except ValueError: continue
    return sorted(mx.items())

def find_carriers(pts, thr):
    if len(pts) < 8: return [], None
    dbs = [d for _, d in pts]; floor = statistics.median(dbs)
    sigs, run = [], []
    for f, d in pts + [(None, -999)]:
        if f is not None and d >= floor + thr: run.append((f, d)); continue
        if run:
            pk = max(run, key=lambda x: x[1]); lin = [10 ** (d / 10) for _, d in run]
            cf = sum(f * w for (f, _), w in zip(run, lin)) / sum(lin)
            step = (pts[1][0] - pts[0][0]) if len(pts) > 1 else 0
            sigs.append({"mhz": round(cf, 5), "peak_db": round(pk[1], 1), "snr": round(pk[1] - floor, 1),
                         "bw_khz": round(max(1, len(run)) * step * 1000, 1)})
            run = []
    return sigs, floor

def _key(mhz): return int(round(mhz * 1000 / 6.25))

def merge_signal(s, band):
    now = time.time(); k = _key(s["mhz"])
    with LOCK:
        r = SIG.get(k) or SIG.get(k - 1) or SIG.get(k + 1)
        if not r:
            r = {"key": k, "mhz": s["mhz"], "band": band, "first": now, "hits": 0, "max_snr": 0, "decoded": None}
            r.update(identify(s["mhz"], s["bw_khz"])); SIG[k] = r
        r.update(last=now, peak_db=s["peak_db"], snr=s["snr"], bw_khz=s["bw_khz"])
        r["hits"] += 1; r["max_snr"] = max(r["max_snr"], s["snr"])
        if not r.get("decoded"): r.update(identify(r["mhz"], s["bw_khz"]))
    return r

def job_sweep(lease, j):
    bands = [b for b in BANDS if CFG["bands"].get(b["id"]) and (not j.get("bands") or b["id"] in j["bands"])]
    found = 0
    for i, b in enumerate(bands):
        if JOB["cancel"]: break
        j["progress"] = "%s (%d/%d)" % (b["name"], i + 1, len(bands)); app.push({"t": "job"})
        pts = sweep_band(lease, b)
        sigs, floor = find_carriers(pts, float(CFG.get("thr_db") or 10))
        step = max(1, len(pts) // 1500)                             # keep the spectrum the UI draws light
        with LOCK: SWEEP["bands"][b["id"]] = {"f": [p[0] for p in pts[::step]], "db": [round(p[1], 1) for p in pts[::step]],
                                             "floor": floor, "t": time.time(), "name": b["name"], "n": len(sigs)}
        for s in sigs: merge_signal(s, b["id"]); found += 1
        app.push({"t": "sweep", "band": b["id"]})
    SWEEP["last"] = time.time()
    log("sweep done: %d carriers above the floor in %d band(s)" % (found, len(bands)))

# ------------------------------------------------------------------ decoders
def new_cap(kind, mhz, title):
    c = {"id": int(time.time() * 1000), "kind": kind, "mhz": mhz, "title": title, "start": time.time(), "end": None,
         "records": [], "devices": {}, "lines": 0}
    with LOCK: CAPS.append(c); del CAPS[:-40]
    app.push({"t": "cap", "id": c["id"]}); return c

def add_rec(c, rec):
    rec["t"] = time.time()
    with LOCK:
        c["records"].append(rec); del c["records"][:-500]
    app.push({"t": "rec", "id": c["id"], "rec": rec})

def _write_log(c):
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(os.path.join(LOGDIR, "%s-%s.jsonl" % (time.strftime("%Y%m%d"), c["kind"])), "a") as f:
            for r in c["records"]: f.write(json.dumps(dict(r, cap=c["title"], mhz=c["mhz"])) + "\n")
    except Exception: pass

def _run_for(p, secs, on_line):
    t0 = time.time()
    def killer():
        while p.poll() is None:
            if JOB["cancel"] or time.time() - t0 > secs: p.terminate(); break
            time.sleep(0.2)
    spawn(killer)
    for raw in p.stdout: on_line(raw.decode(errors="replace").rstrip("\n"))
    p.wait()

def job_multimon(lease, j, demods, title):
    mhz = float(j["mhz"]); c = new_cap(j["dec"], mhz, title)
    rtl = ["rtl_fm", "-d", str(lease.index), "-f", "%.5fM" % mhz, "-M", "fm", "-s", "22050", "-p", str(ppm_for(lease))] + _gain_args() + ["-"]
    mm = ["multimon-ng", "-t", "raw", "-A", "-f", "auto"] + sum((["-a", d] for d in demods), []) + ["-"]
    p1 = subprocess.Popen(rtl, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    p2 = _proc(mm, stdin=p1.stdout); p1.stdout.close()
    hits = {}
    def line(s):
        c["lines"] += 1
        d = co_decode.parse_multimon(s)
        if not d:
            if s.strip() and not s.startswith(("multimon", "Enabled", "Available")) and ":" in s[:12]:
                add_rec(c, {"type": "RAW", "text": s[:200]})
            return
        hits[d["type"]] = hits.get(d["type"], 0) + 1
        if d.get("corrupt"): d["proto"] = d["proto"] + " corrupt"
        if d["type"] in ("POCSAG", "FLEX"):
            d["text"] = "RIC %d%s: %s" % (d["ric"], " fn %d" % d["func"] if d.get("func") is not None else "", d["msg"] or "(%s page)" % d["kind"])
        add_rec(c, d)
    _run_for(p2, float(j.get("secs") or CFG["decode_secs"]), line)
    try: p1.terminate()
    except Exception: pass
    c["end"] = time.time(); _write_log(c)
    tag = {"POCSAG": "POCSAG paging", "FLEX": "FLEX paging", "APRS": "APRS packet", "EAS": "NOAA SAME / EAS"}
    k = _key(mhz)
    with LOCK:
        r = SIG.get(k) or SIG.get(k - 1) or SIG.get(k + 1)
        if r and hits:
            best = max(hits, key=hits.get); r["decoded"] = {"what": tag.get(best, best), "n": hits[best], "t": time.time()}
            r["label"] = tag.get(best, best); r["family"] = {"POCSAG": "paging", "FLEX": "paging"}.get(best, "data")
        elif r and j["dec"] == "probe": r["probed"] = time.time()
    log("%s on %.5f MHz: %s" % (title, mhz, ", ".join("%d %s" % (v, k2) for k2, v in hits.items()) or "nothing decoded"))
    app.push({"t": "cap", "id": c["id"]})

# Utility meters through rtl_433's own decoders: ERT SCM / SCM+ / IDM (Itron-style electric, gas, water) + Neptune R900 water.
# They hop 910-920 MHz; 912.6 MHz at 2.4 MS/s covers the busy middle (rtlamr's default).
METER_PROTOS = [149, 154, 160, 161, 228]
ERT_KIND = {**dict.fromkeys((4, 5, 7, 8), "electric"), **dict.fromkeys((0, 1, 2, 9, 12), "gas"), **dict.fromkeys((3, 11, 13), "water")}

def job_rtl433(lease, j):
    meters = j.get("dec") == "meters"
    mhz = float(j["mhz"]) if j.get("mhz") else (912.6 if meters else None)
    freqs = [mhz] if mhz else [433.92, 315.0, 915.0]
    title = ("Utility meters %.1f MHz" % mhz) if meters else "ISM sensors " + (("%.3f MHz" % mhz) if mhz else "433 / 315 / 915 MHz (hopping)")
    c = new_cap("meters" if meters else "rtl433", mhz, title)
    argv = ["rtl_433", "-d", str(lease.index), "-F", "json", "-M", "level", "-M", "time:unix", "-p", str(ppm_for(lease))]
    for f in freqs: argv += ["-f", "%dk" % round(f * 1000)]
    if len(freqs) > 1: argv += ["-H", str(max(10, int(float(j.get("secs") or CFG["decode_secs"]) / 3)))]
    if meters: argv += ["-s", "2400k"] + sum((["-R", str(p)] for p in METER_PROTOS), [])
    elif any(f > 900 for f in freqs): argv += ["-s", "1024k"]
    g = str(CFG.get("gain", "")).strip().lower()
    if g not in ("", "auto"): argv += ["-g", g]
    p = _proc(argv)
    def line(s):
        c["lines"] += 1
        try: d = json.loads(s)
        except Exception: return
        model = d.get("model", "?"); did = d.get("id", d.get("device", d.get("address", "")))
        key = "%s/%s" % (model, did)
        fields = {k: v for k, v in d.items() if k not in ("time", "model", "id", "mic", "mod", "freq", "freq1", "freq2", "rssi", "snr", "noise")}
        if meters:                                         # utility: electric / gas / water from the ERT type
            et = d.get("ert_type", d.get("ERTType", d.get("EndpointType")))
            fields = {"utility": "water" if "R900" in model or "Neptune" in model else ERT_KIND.get(et, "ERT type %s" % et if et is not None else "?"), **fields}
        with LOCK:
            dv = c["devices"].setdefault(key, {"model": model, "id": did, "n": 0, "first": time.time()})
            dv.update(n=dv["n"] + 1, last=time.time(), fields=fields, rssi=d.get("rssi"), snr=d.get("snr"),
                      freq=d.get("freq") or d.get("freq1"), mic=d.get("mic"))
        add_rec(c, {"type": "ISM", "model": model, "dev_id": did, "fields": fields, "rssi": d.get("rssi"),
                    "text": "%s #%s %s" % (model, did, ", ".join("%s=%s" % (k, v) for k, v in list(fields.items())[:6]))})
    _run_for(p, float(j.get("secs") or CFG["decode_secs"]), line)
    c["end"] = time.time(); _write_log(c)
    if mhz:
        k = _key(mhz)
        with LOCK:
            r = SIG.get(k) or SIG.get(k - 1) or SIG.get(k + 1)
            if r and c["devices"]: r["decoded"] = {"what": "%d ISM device(s)" % len(c["devices"]), "n": len(c["records"]), "t": time.time()}
    log("%s: %d device(s), %d message(s)" % (title, len(c["devices"]), len(c["records"])))
    app.push({"t": "cap", "id": c["id"]})

def job_adsb(lease, j):
    c = new_cap("adsb", 1090.0, "ADS-B aircraft 1090 MHz")
    d = tempfile.mkdtemp(prefix="co-spy-adsb-")
    argv = ["readsb", "--device-type", "rtlsdr", "--device", str(lease.index), "--write-json", d, "--write-json-every", "1", "--quiet",
            "--ppm", str(ppm_for(lease))] + (["--gain", str(CFG["gain"])] if str(CFG.get("gain", "")).lower() not in ("", "auto") else ["--gain", "auto"])
    p = _proc(argv, stdin=subprocess.DEVNULL); t0 = time.time(); secs = float(j.get("secs") or CFG["decode_secs"])
    while p.poll() is None and not JOB["cancel"] and time.time() - t0 < secs:
        time.sleep(1.5)
        try: ac = json.load(open(os.path.join(d, "aircraft.json"))).get("aircraft", [])
        except Exception: continue
        with LOCK:
            for a in ac:
                h = a.get("hex"); dv = c["devices"].setdefault(h, {"hex": h, "first": time.time(), "n": 0})
                dv.update(n=dv["n"] + 1, last=time.time(), flight=(a.get("flight") or "").strip() or dv.get("flight"),
                          alt=a.get("alt_baro", dv.get("alt")), gs=a.get("gs", dv.get("gs")), track=a.get("track", dv.get("track")),
                          lat=a.get("lat", dv.get("lat")), lon=a.get("lon", dv.get("lon")), rssi=a.get("rssi"), squawk=a.get("squawk", dv.get("squawk")),
                          category=a.get("category", dv.get("category")), msgs=a.get("messages"))
        app.push({"t": "cap", "id": c["id"]})
    try: p.terminate(); p.wait(3)
    except Exception: pass
    shutil.rmtree(d, ignore_errors=True)
    c["end"] = time.time(); c["records"] = [dict(v, type="ADSB", text="%s %s alt %s" % (v["hex"], v.get("flight") or "", v.get("alt"))) for v in c["devices"].values()]
    _write_log(c); log("ADS-B: %d aircraft" % len(c["devices"])); app.push({"t": "cap", "id": c["id"]})

# ------------------------------------------------------------------ AIS (AIS-catcher) + ACARS (acarsdec)
AIS_WEB = 8100                                             # AIS-catcher's own map / ship list while a job runs
AIS_CLASS = {**dict.fromkeys((1, 2, 3, 5), "Class A"), **dict.fromkeys((18, 19, 24), "Class B"), 4: "Base station", 21: "Aid to navigation",
             9: "SAR aircraft", 27: "Class A (long range)"}

def job_ais(lease, j):
    c = new_cap("ais", 162.0, "AIS ships 161.975 / 162.025 MHz"); c["web"] = AIS_WEB
    g = str(CFG.get("gain", "")).strip().lower()
    argv = ["AIS-catcher"] + (["-d", lease.serial] if lease.serial else ["-d:%d" % lease.index]) + ["-gr", "TUNER", "auto" if g in ("", "auto") else g, "RTLAGC", "off",
            "-p", str(ppm_for(lease)), "-o", "5", "-M", "D", "-N", str(AIS_WEB)]
    p = _proc(argv, stdin=subprocess.DEVNULL)
    def line(s):
        c["lines"] += 1
        i = s.find('{"class"')                             # the startup banner shares stdout, sometimes without a newline
        if i < 0: return
        try: d = json.loads(s[i:])
        except Exception: return
        mm = d.get("mmsi")
        if mm is None: return
        t = d.get("type")
        with LOCK:
            v = c["devices"].setdefault(str(mm), {"mmsi": mm, "n": 0, "first": time.time()})
            v.update(n=v["n"] + 1, last=time.time(), cls=AIS_CLASS.get(t, v.get("cls")), rssi=d.get("signalpower"))
            for k, src in (("name", "shipname"), ("name", "name"), ("callsign", "callsign"), ("shiptype", "shiptype_text"), ("shiptype", "aid_type_text"),
                           ("dest", "destination"), ("status", "status_text"), ("lat", "lat"), ("lon", "lon"), ("course", "course"), ("heading", "heading")):
                x = d.get(src)
                if x not in (None, "") and not (k in ("lat", "lon") and abs(x) > 90 + 90 * (k == "lon")): v[k] = x.strip() if isinstance(x, str) else x
            if d.get("speed") is not None and d["speed"] < 102.3: v["speed"] = round(d["speed"], 1)
        if t in (1, 2, 3, 5, 18, 19, 24) and v["n"] == 1:
            add_rec(c, {"type": "AIS", "text": "%s %s %s" % (mm, v.get("name") or "", v.get("cls") or "")})
    _run_for(p, float(j.get("secs") or max(120, CFG["decode_secs"])), line)
    c["end"] = time.time(); c["records"] = [dict(v, type="AIS", text="%s %s %s" % (v["mmsi"], v.get("name") or "", v.get("cls") or "")) for v in c["devices"].values()]
    _write_log(c); log("AIS: %d station(s) (%d ship(s))" % (len(c["devices"]), sum(1 for v in c["devices"].values() if v.get("cls", "").startswith("Class"))))
    app.push({"t": "cap", "id": c["id"]})

# 2.4 MS/s spans 130.025-131.55; 129.125 is "too far apart" for one tuning, so it's left out.
ACARS_FREQS = ["130.025", "130.450", "131.125", "131.550"]
ACARS_LABEL = {"_d": "ack / no info", "Q0": "link test", "SQ": "ground station squitter", "H1": "flight deck / FMS message",
               "SA": "media advisory", "5Z": "airline designated", "80": "airline designated downlink", "QP": "OOOI: out of gate",
               "QQ": "OOOI: off (takeoff)", "QR": "OOOI: on (landing)", "QS": "OOOI: in at gate", "B6": "ADS-C", "BA": "CPDLC", "AA": "CPDLC", "A6": "ADS-C request"}

def job_acars(lease, j):
    c = new_cap("acars", 131.55, "ACARS " + " / ".join(ACARS_FREQS) + " MHz")
    g = str(CFG.get("gain", "")).strip().lower()
    argv = ["acarsdec", "-o", "4", "-m", "192", "-g", "-10" if g in ("", "auto") else g, "-p", str(ppm_for(lease)),
            "-r", lease.serial or str(lease.index)] + ACARS_FREQS
    p = _proc(argv, stdin=subprocess.DEVNULL)
    def line(s):
        c["lines"] += 1
        try: d = json.loads(s)
        except Exception: return
        tail = (d.get("tail") or "").strip(".") or "?"; lab = d.get("label") or ""
        with LOCK:
            v = c["devices"].setdefault(tail, {"tail": tail, "n": 0, "first": time.time()})
            v.update(n=v["n"] + 1, last=time.time(), flight=d.get("flight") or v.get("flight"), label=ACARS_LABEL.get(lab, lab) if lab != "_d" else v.get("label"))
        if lab == "_d" and not d.get("text"): return            # empty acks: counted per aircraft, kept out of the feed
        la = d.get("libacars")
        add_rec(c, {"type": "ACARS", "tail": tail, "flight": d.get("flight"), "label": lab + ("/" + d["sublabel"] if d.get("sublabel") else ""),
                    "label_txt": ACARS_LABEL.get(lab, ""), "freq": d.get("freq"), "level": d.get("level"), "text": d.get("text") or "",
                    "decoded": json.dumps(la, separators=(",", ":"))[:600] if la else None})
    _run_for(p, float(j.get("secs") or max(180, CFG["decode_secs"])), line)
    c["end"] = time.time(); _write_log(c)
    log("ACARS: %d aircraft, %d message(s)" % (len(c["devices"]), len(c["records"]))); app.push({"t": "cap", "id": c["id"]})

VDL2_FREQS = ["136650000", "136700000", "136725000", "136775000", "136800000", "136975000"]   # US VDL2 channels, one 1.05 MS/s tuning

_VDL_STD = ("err", "crc_ok", "more", "reg", "mode", "label", "blk_id", "ack", "flight", "msg_num", "msg_num_seq", "msg_text", "sublabel")
def _vdl_extra(ac):
    x = {k: v for k, v in ac.items() if k not in _VDL_STD}               # libacars-decoded payloads (ARINC 622, CPDLC, ADS-C...)
    return json.dumps(x, separators=(",", ":"))[:600] if x else None

def job_vdl2(lease, j):
    """dumpvdl2 (VDL Mode 2): aircraft by ICAO address, ACARS carried over AVLC, logons (XID)."""
    c = new_cap("vdl2", 136.8, "VDL2 136.650-136.975 MHz")
    g = str(CFG.get("gain", "")).strip().lower()
    argv = ["dumpvdl2", "--rtlsdr", lease.serial or str(lease.index), "--correction", str(ppm_for(lease)),
            "--output", "decoded:json:file:path=-"] + ([] if g in ("", "auto") else ["--gain", g]) + VDL2_FREQS
    p = _proc(argv, stdin=subprocess.DEVNULL)
    def line(s):
        c["lines"] += 1
        try: v = json.loads(s)["vdl2"]
        except Exception: return
        a = v.get("avlc") or {}; src = a.get("src") or {}; dst = a.get("dst") or {}
        air = src if src.get("type") == "Aircraft" else dst if dst.get("type") == "Aircraft" else None
        ac = a.get("acars") or {}
        if air:
            with LOCK:
                d = c["devices"].setdefault(air["addr"], {"hex": air["addr"], "n": 0, "first": time.time()})
                d.update(n=d["n"] + 1, last=time.time(), status=air.get("status") or d.get("status"),
                         tail=(ac.get("reg") or "").strip(".") or d.get("tail"), flight=ac.get("flight") or d.get("flight"))
                if ac.get("label") and ac["label"] != "_d": d["label"] = ACARS_LABEL.get(ac["label"], ac["label"])
        lab = ac.get("label") or ""
        if ac and not (lab == "_d" and not ac.get("msg_text")):
            add_rec(c, {"type": "VDL2", "tail": (ac.get("reg") or "").strip(".") or (air or {}).get("addr"), "flight": ac.get("flight"),
                        "label": lab + ("/" + ac["sublabel"] if ac.get("sublabel") else ""), "label_txt": ACARS_LABEL.get(lab, ""),
                        "freq": v.get("freq", 0) / 1e6, "level": round(v.get("sig_level", 0), 1), "text": ac.get("msg_text") or "",
                        "decoded": _vdl_extra(ac)})
        elif "xid" in a and air:
            add_rec(c, {"type": "VDL2", "tail": air["addr"], "label": "XID", "label_txt": "link setup / logon (%s)" % (src.get("status") or "?"),
                        "freq": v.get("freq", 0) / 1e6, "level": round(v.get("sig_level", 0), 1), "text": ""})
    _run_for(p, float(j.get("secs") or max(180, CFG["decode_secs"])), line)
    c["end"] = time.time(); _write_log(c)
    log("VDL2: %d aircraft, %d message(s)" % (len(c["devices"]), len(c["records"]))); app.push({"t": "cap", "id": c["id"]})

# ------------------------------------------------------------------ weather satellites (SatDump, Meteor-M LRPT)
# NOAA-15/18/19 APT ended in 2025; Meteor-M N2-3 / N2-4 send LRPT (72k QPSK) on 137.9 (primary) / 137.1 MHz.
# Passes come from Celestrak's weather TLEs (cached, refreshed daily) + skyfield + the GPS position.
WXDIR = os.path.join(LOGDIR, "wxsat")
TLE_PATH = os.path.join(LOGDIR, "weather.tle")
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle"
CFG.setdefault("wx", {"sats": {"METEOR-M2 3": 137.9, "METEOR-M2 4": 137.9}, "min_el": 20, "auto": False})
WX = {"passes": [], "t": 0, "err": None, "loc": None, "queued": set()}

def _tle():
    if not os.path.exists(TLE_PATH) or time.time() - os.path.getmtime(TLE_PATH) > 86400:
        try:
            import urllib.request
            data = urllib.request.urlopen(TLE_URL, timeout=15).read().decode()
            if "METEOR" in data: os.makedirs(LOGDIR, exist_ok=True); open(TLE_PATH, "w").write(data)
        except Exception as e:
            if not os.path.exists(TLE_PATH): raise RuntimeError("no TLEs yet (needs internet once): %s" % e)
    L = [l.rstrip() for l in open(TLE_PATH) if l.strip()]
    return {L[i].strip(): (L[i + 1], L[i + 2]) for i in range(0, len(L) - 2, 3) if L[i + 1].startswith("1 ")}, os.path.getmtime(TLE_PATH)

def _where():
    try:
        s = socket.create_connection(("127.0.0.1", 2947), timeout=2); s.sendall(b'?WATCH={"enable":true,"json":true}\n'); s.settimeout(3); buf = b""
        for _ in range(30):
            buf += s.recv(4096)
            for ln in buf.split(b"\n"):
                try: m = json.loads(ln)
                except Exception: continue
                if m.get("class") == "TPV" and m.get("mode", 0) >= 2: return round(m["lat"], 4), round(m["lon"], 4), "GPS"
    except Exception: pass
    try:
        t = open(os.path.expanduser("~/radiosonde_auto_rx/auto_rx/station.cfg")).read()
        la, lo = (float(re.search(r"(?m)^%s\s*=\s*([-\d.]+)" % k, t).group(1)) for k in ("station_lat", "station_lon"))
        if la or lo: return la, lo, "saved"
    except Exception: pass
    return None

def wx_passes(force=False):
    if not force and time.time() - WX["t"] < 600: return WX
    try:
        from skyfield.api import load, EarthSatellite, wgs84
        tles, tage = _tle(); loc = _where() or WX["loc"]
        if not loc: raise RuntimeError("no location: GPS has no fix and none is saved")
        ts = load.timescale(); here = wgs84.latlon(loc[0], loc[1]); t0 = ts.now(); t1 = ts.tt_jd(t0.tt + 1.0)
        out = []
        for name, mhz in CFG["wx"]["sats"].items():
            if name not in tles: continue
            sat = EarthSatellite(*tles[name], name, ts)
            t, ev = sat.find_events(here, t0, t1, altitude_degrees=5.0)
            cur = {}
            for ti, e in zip(t, ev):
                if e == 0: cur = {"sat": name, "mhz": mhz, "aos": ti.utc_datetime().timestamp()}
                elif e == 1 and cur:
                    alt, az, _ = (sat - here).at(ti).altaz(); cur.update(max_el=round(alt.degrees, 1), az=round(az.degrees))
                elif e == 2 and cur.get("max_el") is not None:
                    cur["los"] = ti.utc_datetime().timestamp(); out.append(cur); cur = {}
        out.sort(key=lambda p: p["aos"])
        WX.update(passes=out, t=time.time(), err=None, loc=loc, tle_age=time.time() - tage)
    except Exception as e:
        WX.update(err="%s" % e, t=time.time())
    return WX

def job_wxsat(lease, j):
    sat, mhz = j.get("sat") or "test", float(j.get("mhz") or 137.9)
    out = os.path.join(WXDIR, time.strftime("%Y%m%d-%H%M") + "-" + re.sub(r"\W+", "", sat)); os.makedirs(out, exist_ok=True)
    secs = int(j.get("secs") or 900)
    c = new_cap("wxsat", mhz, "Weather sat %s %.1f MHz" % (sat, mhz)); c["dir"] = out
    g = str(CFG.get("gain", "")).strip().lower()
    argv = ["satdump", "live", "meteor_m2-x_lrpt", out, "--source", "rtlsdr", "--source_id", lease.serial or str(lease.index),
            "--samplerate", "1.024e6", "--frequency", "%.4fe6" % mhz, "--gain", "40" if g in ("", "auto") else g,
            "--timeout", str(secs)]
    p = _proc(argv, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    def line(s):
        c["lines"] += 1
        s = _ANSI.sub("", s)
        m = re.search(r"SNR\s*:?\s*([\d.]+)", s) or re.search(r"Deframer.*(SYNCED|NOSYNC)", s)
        if m: c["status"] = s.split(") ", 1)[-1][:120]
    _run_for(p, secs + 120, line)                           # + time for SatDump to build the image products
    imgs = sorted(os.path.relpath(f, WXDIR) for f in glob.glob(os.path.join(out, "**", "*.png"), recursive=True))
    c["images"] = imgs; c["end"] = time.time(); _write_log(c)
    log("Weather sat %s: %d image(s) in %s" % (sat, len(imgs), out)); app.push({"t": "cap", "id": c["id"]})

def wx_scheduler():
    """Auto-record: queue a SatDump job ~1 min before AOS of every pass at or above min_el."""
    while True:
        time.sleep(30)
        if not CFG["wx"].get("auto"): continue
        for p in wx_passes()["passes"]:
            k = "%s-%d" % (p["sat"], p["aos"])
            if p["max_el"] >= float(CFG["wx"]["min_el"]) and 0 < p["aos"] - time.time() < 90 and k not in WX["queued"]:
                WX["queued"].add(k); enqueue({"kind": "decode", "dec": "wxsat", "sat": p["sat"], "mhz": p["mhz"], "secs": int(p["los"] - time.time()) + 30})
                log("auto-record queued: %s, max %.0f°" % (p["sat"], p["max_el"]))

# ------------------------------------------------------------------ digital voice (dsd-fme)
# Auto-detect P25 p1/p2, DMR, NXDN, D-STAR, YSF, dPMR, X2-TDMA on one carrier, no audio. Sync lines name the protocol;
# everything else dsd-fme prints (system IDs, talkgroups, radio IDs, CSBK/TSBK...) is kept once per distinct line.
_DSYNC = re.compile(r"Sync:\s*[+-](P25p1|P25p2|DMR|NXDN48|NXDN96|DSTAR|YSF|dPMR|M17|X2-TDMA|EDACS|ProVoice)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_DKEEP = re.compile(r"TGT|SRC|TG |WACN|SYSID|NAC|RFSS|SITE|Color Code|CC |RAN|Talkgroup|Radio|Group|Private|Voice|Data|Encrypt|ALG|Key|CSBK|TSBK|Site|Capacity|Connect|Hytera|Moto|Kenwood|Call|Alias|GPS|LRRP|Text", re.I)

def job_dsd(lease, j):
    mhz = float(j["mhz"]); c = new_cap("dsd", mhz, "Digital voice (dsd-fme)")
    g = str(CFG.get("gain", "")).strip().lower()
    argv = ["dsd-fme", "-fa", "-o", "null", "-i", "rtl:%d:%.5fM:%s:%d:12:0:2" % (lease.index, mhz, "0" if g in ("", "auto") else g, ppm_for(lease))]
    p = _proc(argv, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    hits, seen = {}, {}
    def line(s):
        c["lines"] += 1
        s = _ANSI.sub("", s).strip()
        m = _DSYNC.search(s)
        if m:
            hits[m.group(1)] = hits.get(m.group(1), 0) + 1
            if "FEC ERR" in s: hits["_fec"] = hits.get("_fec", 0) + 1
            return
        if not s or not _DKEEP.search(s) or s.startswith(("Decoding", "Setting", "Digital Speech", "Audio", "Build")): return
        k = re.sub(r"^\d\d:\d\d:\d\d\s*", "", s)[:160]
        if k in seen: seen[k]["n"] += 1; return
        seen[k] = {"type": "DSD", "proto": max((x for x in hits if x != "_fec"), key=hits.get, default="DSD"), "text": k, "n": 1}
        add_rec(c, seen[k])
    _run_for(p, float(j.get("secs") or CFG["decode_secs"]), line)
    c["end"] = time.time(); c["sync"] = hits; _write_log(c)
    proto = max((x for x in hits if x != "_fec"), key=hits.get, default=None)
    k = _key(mhz)
    with LOCK:
        r = SIG.get(k) or SIG.get(k - 1) or SIG.get(k + 1)
        if r and proto and hits[proto] >= 3:
            r["decoded"] = {"what": proto, "n": hits[proto], "t": time.time()}; r["label"] = proto; r["family"] = "voice"
        elif r: r["probed"] = time.time()
    log("Digital voice on %.5f MHz: %s" % (mhz, ", ".join("%d %s sync" % (v, x) for x, v in hits.items() if x != "_fec") + (" (%d with FEC errors)" % hits["_fec"] if hits.get("_fec") else "") or "no digital sync"))
    app.push({"t": "cap", "id": c["id"]})

MM = {"pager": (["POCSAG512", "POCSAG1200", "POCSAG2400", "FLEX"], "Pager decode"),
      "aprs": (["AFSK1200"], "APRS decode"), "eas": (["EAS"], "NOAA SAME / EAS"),
      "probe": (["POCSAG512", "POCSAG1200", "POCSAG2400", "FLEX", "AFSK1200", "EAS"], "Probe")}

def run_job(j):
    lease, why = co_radio.lease("System Spy", prefer=CFG.get("device"))
    if not lease:
        log("no radio: " + why); return
    with LOCK: j["device"] = lease.describe()
    app.push({"t": "job"})
    try:
        if j["kind"] == "sweep": job_sweep(lease, j)
        elif j["kind"] == "identify":                      # probe each unidentified strong carrier in turn
            todo = [r for r in sorted(SIG.values(), key=lambda r: -r.get("max_snr", 0))
                    if r.get("decoder") in ("probe", "pager") and not r.get("decoded") and not r.get("probed")][:int(j.get("n") or 6)]
            for i, r in enumerate(todo):
                if JOB["cancel"]: break
                j["progress"] = "%.5f MHz (%d/%d)" % (r["mhz"], i + 1, len(todo)); app.push({"t": "job"})
                job_multimon(lease, {"mhz": r["mhz"], "dec": "probe", "secs": j.get("secs") or 12}, MM["probe"][0], "Probe")
            if not todo: log("nothing left to identify: sweep first, or every carrier has been probed")
        elif j["kind"] == "decode":
            if j["dec"] in MM: job_multimon(lease, j, *MM[j["dec"]])
            elif j["dec"] in ("rtl433", "meters"): job_rtl433(lease, j)
            elif j["dec"] == "adsb": job_adsb(lease, j)
            elif j["dec"] == "ais": job_ais(lease, j)
            elif j["dec"] == "acars": job_acars(lease, j)
            elif j["dec"] == "dsd": job_dsd(lease, j)
            elif j["dec"] == "vdl2": job_vdl2(lease, j)
            elif j["dec"] == "wxsat": job_wxsat(lease, j)
    except Exception as e:
        log("job error: %s: %s" % (type(e).__name__, e))
    finally:
        lease.release()

def worker():
    while True:
        j = JOBQ.get()
        with LOCK:
            if j in JOB["q"]: JOB["q"].remove(j)
            JOB.update(cur=j, cancel=False)
        app.push({"t": "job"})
        run_job(j)
        with LOCK: JOB.update(cur=None, proc=None)
        app.push({"t": "job"})
        if j.get("repeat") and not JOB.get("halt"):
            j2 = dict(j, progress=None); JOB["q"].append(j2); JOBQ.put(j2)

def enqueue(j):
    j.setdefault("progress", None); j["queued"] = time.time()
    with LOCK: JOB["halt"] = False; JOB["q"].append(j)
    JOBQ.put(j); app.push({"t": "job"})

# ------------------------------------------------------------------ nRF52840 802.15.4 sniffer (Zigbee / Thread)
def nrf_find():
    """ttyACM of a Nordic (VID 1915) dongle, preferring one whose USB product names the 802.15.4 sniffer."""
    best = None
    for tty in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        dev = os.path.realpath(os.path.join(tty, "device", ".."))
        try: vid = open(os.path.join(dev, "idVendor")).read().strip()
        except Exception: continue
        prod = ""
        try: prod = open(os.path.join(dev, "product")).read().strip()
        except Exception: pass
        if vid == "1915":
            cand = {"port": "/dev/" + os.path.basename(tty), "product": prod, "sniffer": "802.15.4" in prod or "Sniffer" in prod}
            if cand["sniffer"] or not best: best = cand
    return best

_RX = re.compile(r"received:\s+([0-9a-fA-F]+)\s+power:\s+(-?\d+)\s+lqi:\s+(\d+)\s+time:\s+(-?\d+)")

def _nrf_open(port):
    import serial
    s = serial.Serial(port, 115200, timeout=0.3)
    s.write(b"sleep\r\n"); time.sleep(0.1); s.reset_input_buffer(); return s

def _nrf_frame(ch, hexs, rssi, lqi):
    d = co_decode.parse_154(hexs)
    if not d: return
    d.update(ch=ch, rssi=int(rssi), lqi=int(lqi), t=time.time())
    with LOCK:
        NRF["count"] += 1; NRF["frames"].append(d); del NRF["frames"][:-400]
        sv = NRF["survey"].setdefault(str(ch), {"frames": 0, "rssi": None, "fam": {}})
        sv["frames"] += 1; sv["rssi"] = max(sv["rssi"] or -999, d["rssi"]); sv["fam"][d["family"]] = sv["fam"].get(d["family"], 0) + 1
        pan = d.get("pan")
        if pan and pan != "0xffff":
            n = NRF["nets"].setdefault("%s@%d" % (pan, ch), {"pan": pan, "ch": ch, "family": d["family"], "frames": 0, "devs": [],
                                                             "secure": False, "first": time.time()})
            n["frames"] += 1; n["last"] = time.time(); n["rssi"] = d["rssi"]
            if d["family"] != "802.15.4": n["family"] = d["family"]
            n["secure"] = n["secure"] or d["secure"] or d.get("nwk_secure", False)
            for k in ("ext_pan", "net_name", "stack", "permit_join"):
                if d.get(k) is not None: n[k] = d[k]
            for a in (d.get("src"), d.get("dst"), d.get("nwk_src"), d.get("nwk_dst")):
                if a and a not in ("0xffff", "0xfffd", "0xfffc") and a not in n["devs"] and len(n["devs"]) < 200: n["devs"].append(a)
    app.push({"t": "frame", "f": d})

def _nrf_listen(s, ch, secs):
    s.write(b"sleep\r\n"); time.sleep(0.05); s.write(b"channel %d\r\n" % ch); time.sleep(0.05); s.write(b"receive\r\n")
    NRF["chan"] = ch; t0 = time.time(); buf = b""
    while not NRF["stop"] and (secs is None or time.time() - t0 < secs):
        buf += s.read(4096)
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            m = _RX.search(ln.decode(errors="replace"))
            if m: _nrf_frame(ch, *m.groups()[:3])
    s.write(b"sleep\r\n")

def nrf_job(mode, ch=None):
    dv = nrf_find()
    if not dv:
        NRF.update(state="idle", err="No nRF52840 dongle found (Nordic USB VID 1915 on /dev/ttyACM*)"); app.push({"t": "nrf"}); return
    NRF.update(port=dv["port"], err=None, stop=False, state=mode)
    app.push({"t": "nrf"})
    try:
        s = _nrf_open(dv["port"])
        if mode == "survey":
            with LOCK: NRF["survey"] = {}
            for c in range(11, 27):
                if NRF["stop"]: break
                _nrf_listen(s, c, float(CFG.get("dwell_154") or 3)); app.push({"t": "nrf"})
            busy = sorted(NRF["survey"].items(), key=lambda x: -x[1]["frames"])
            log("2.4 GHz survey: %s" % (", ".join("ch %s %d frames" % (k, v["frames"]) for k, v in busy[:4] if v["frames"]) or "no 802.15.4 traffic heard"))
        else:
            _nrf_listen(s, int(ch or 15), None)
        s.close()
    except Exception as e:
        NRF["err"] = "%s: %s (is the 802.15.4 sniffer firmware flashed?)" % (type(e).__name__, e)
    NRF.update(state="idle", chan=None); app.push({"t": "nrf"})

# ------------------------------------------------------------------ API
def sig_list():
    with LOCK: return sorted((dict(r) for r in SIG.values()), key=lambda r: r["mhz"])

@app.get("/api/state")
def api_state(q):
    with LOCK:
        cur = JOB["cur"] and {k: JOB["cur"].get(k) for k in ("kind", "dec", "mhz", "progress", "device", "repeat", "queued")}
        jobs = [{k: x.get(k) for k in ("kind", "dec", "mhz")} for x in JOB["q"]]
        caps = [{k: c[k] for k in ("id", "kind", "mhz", "title", "start", "end", "lines")} | {"n": len(c["records"]), "devs": len(c["devices"])} for c in CAPS]
        nrf = {k: NRF[k] for k in ("port", "state", "chan", "count", "err")}
        nrf["nets"] = sorted(NRF["nets"].values(), key=lambda n: -n["frames"]); nrf["survey"] = NRF["survey"]
    nrf["dongle"] = nrf_find()
    return {"cfg": CFG, "bands": BANDS, "decoders": DECODERS, "signals": sig_list(), "job": cur, "queue": jobs, "caps": caps,
            "log": JOB["log"][-30:], "radios": co_radio.status(), "sweep_last": SWEEP["last"], "nrf": nrf}

@app.get("/api/spectrum")
def api_spectrum(q):
    with LOCK: return SWEEP["bands"].get(q.get("band", ""), {}) if q.get("band") else {k: {"name": v["name"], "n": v["n"], "t": v["t"]} for k, v in SWEEP["bands"].items()}

@app.get("/api/cap")
def api_cap(q):
    with LOCK:
        c = next((c for c in CAPS if str(c["id"]) == q.get("id")), None)
        return dict(c, records=c["records"][-300:]) if c else (404, {"ok": False})

@app.get("/api/frames")
def api_frames(q):
    with LOCK: return NRF["frames"][-int(q.get("n") or 200):]

@app.post("/api/sweep")
def api_sweep(q, b): enqueue({"kind": "sweep", "bands": b.get("bands"), "repeat": bool(b.get("repeat"))}); return {"ok": True}

@app.post("/api/identify")
def api_identify(q, b): enqueue({"kind": "identify", "n": b.get("n") or 6, "secs": b.get("secs") or 12}); return {"ok": True}

@app.post("/api/decode")
def api_decode(q, b):
    if b.get("dec") not in ("pager", "aprs", "eas", "probe", "rtl433", "adsb", "meters", "ais", "acars", "dsd", "vdl2", "wxsat"): return 400, {"ok": False, "err": "unknown decoder"}
    enqueue({"kind": "decode", "dec": b["dec"], "mhz": b.get("mhz"), "secs": b.get("secs"), "sat": b.get("sat")}); return {"ok": True}

@app.post("/api/stop")
def api_stop(q, b):
    with LOCK:
        JOB["halt"] = True; JOB["cancel"] = True; JOB["q"].clear()
        while not JOBQ.empty():
            try: JOBQ.get_nowait()
            except queue.Empty: break
    _stop_proc(); return {"ok": True}

@app.post("/api/signals/clear")
def api_sclear(q, b):
    with LOCK: SIG.clear(); SWEEP["bands"].clear()
    return {"ok": True}

@app.post("/api/caps/clear")
def api_cclear(q, b):
    with LOCK: CAPS.clear()
    return {"ok": True}

@app.get("/api/wx")
def api_wx(q):
    w = wx_passes(force=q.get("refresh") == "1")
    recs = sorted((d for d in glob.glob(os.path.join(WXDIR, "*")) if os.path.isdir(d)), reverse=True)[:20]
    return {"passes": [p for p in w["passes"] if p["los"] > time.time()], "err": w["err"], "loc": w["loc"], "tle_age": w.get("tle_age"),
            "cfg": CFG["wx"], "recordings": [{"dir": os.path.basename(d), "images": sorted(os.path.relpath(f, WXDIR) for f in glob.glob(os.path.join(d, "**", "*.png"), recursive=True))} for d in recs]}

@app.post("/api/wx/settings")
def api_wx_settings(q, b):
    if "auto" in b: CFG["wx"]["auto"] = bool(b["auto"])
    if "min_el" in b: CFG["wx"]["min_el"] = max(5, min(80, float(b["min_el"])))
    for k, v in (b.get("sats") or {}).items():
        if k in CFG["wx"]["sats"] and float(v) in (137.1, 137.9): CFG["wx"]["sats"][k] = float(v); WX["t"] = 0
    save_cfg(); return {"ok": True, "cfg": CFG["wx"]}

@app.get("/api/wx/img/")
def api_wx_img(q):
    f = os.path.realpath(os.path.join(WXDIR, q.get("_rest", "")))
    if not f.startswith(os.path.realpath(WXDIR) + os.sep) or not f.endswith(".png") or not os.path.isfile(f): return 404, {"ok": False}
    with open(f, "rb") as fh: return fh.read(), "image/png"

@app.post("/api/settings")
def api_settings(q, b):
    with LOCK:
        for k, f in (("thr_db", float), ("gain", str), ("ppm", lambda v: None if v in (None, "") else float(v)), ("device", lambda v: v or None),
                     ("decode_secs", int), ("dwell_154", float), ("hold_s", int)):
            if k in b: CFG[k] = f(b[k])
        if isinstance(b.get("bands"), dict): CFG["bands"].update({k: bool(v) for k, v in b["bands"].items()})
    save_cfg(); return {"ok": True}

@app.post("/api/nrf/survey")
def api_nrf_survey(q, b):
    if NRF["state"] != "idle": return 409, {"ok": False, "err": "sniffer busy"}
    spawn(nrf_job, "survey"); return {"ok": True}

@app.post("/api/nrf/monitor")
def api_nrf_mon(q, b):
    if NRF["state"] != "idle": return 409, {"ok": False, "err": "sniffer busy"}
    ch = int(b.get("ch") or 15)
    if not 11 <= ch <= 26: return 400, {"ok": False, "err": "channel 11-26"}
    spawn(nrf_job, "monitor", ch); return {"ok": True}

@app.post("/api/nrf/stop")
def api_nrf_stop(q, b): NRF["stop"] = True; return {"ok": True}

@app.post("/api/nrf/clear")
def api_nrf_clear(q, b):
    with LOCK: NRF.update(nets={}, frames=[], survey={}, count=0)
    return {"ok": True}

if __name__ == "__main__":
    spawn(worker); spawn(wx_scheduler)
    app.serve()
