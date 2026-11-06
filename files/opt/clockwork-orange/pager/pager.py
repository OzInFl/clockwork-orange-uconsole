#!/usr/bin/env python3
"""Clockwork Orange Pager Recorder - POCSAG 512/1200/2400 + FLEX recorder with per-RIC alerts.

rtl_fm (FM, 22.05 kHz) -> multimon-ng (POCSAG512/1200/2400 + FLEX) -> SQLite (~/co-pager/pages.db), every page kept.
The watch list maps RICs/capcodes to a label and one of 8 alert sounds; normal view shows watched pages, debug shows all.
Alerts sound on the uConsole speaker (pw-play, works with the window closed) and/or in the browser (/sound/N.wav).
Radio from co_radio: a second USB RTL if present, else the AIO RTL borrowed from Looking Glass (resumed on stop).
Web UI + API on :8091. Receive only.
"""
import io, math, os, re, sqlite3, struct, subprocess, sys, threading, time, wave
sys.path.insert(0, "/opt/clockwork-orange/lib"); sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
import co_radio, co_decode
from co_web import App, spawn, load_json, save_json

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("CO_PAGER_PORT") or 8091)
CFG_PATH = os.path.expanduser("~/.config/clockwork-orange/pager.json")
DATA = os.path.expanduser(os.environ.get("CO_PAGER_DATA") or "~/co-pager")
SOUNDS = os.path.join(DATA, "sounds")
MAX_ROWS = 200000
DEFAULTS = {"freq": 152.0075, "gain": "40", "ppm": None, "squelch": 0, "prune": False, "invert": False, "debug": False,
            "sound_device": True, "sound_browser": True, "volume": 0.8, "dedupe_s": 60, "record_on_boot": False,
            "device": None, "watch": []}
SOUND_NAMES = ["Chirp", "Two-tone", "Triple beep", "Klaxon", "Siren", "Bell", "Warble", "Alarm"]

app = App(PORT, os.path.join(HERE, "index.html"))
LOCK = threading.RLock()
CFG = dict(DEFAULTS, **load_json(CFG_PATH, {}))
ST = {"want": False, "gen": 0, "reload": False, "release": False, "running": False, "error": None, "device": None, "borrowed": False, "since": None,
      "freq": None, "lines": 0, "pages": 0, "by_proto": {}, "last_page": None, "procs": None}

def save_cfg():
    with LOCK: save_json(CFG_PATH, CFG)

# ------------------------------------------------------------------ storage
os.makedirs(DATA, exist_ok=True)
def db():
    c = sqlite3.connect(os.path.join(DATA, "pages.db"), timeout=10); c.row_factory = sqlite3.Row; return c
with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS pages (id INTEGER PRIMARY KEY, t REAL, proto TEXT, ric INTEGER, rics TEXT, func INTEGER,
                 kind TEXT, msg TEXT, freq REAL, watched INTEGER, dup INTEGER DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS pages_ric ON pages(ric)"); c.execute("CREATE INDEX IF NOT EXISTS pages_t ON pages(t)")

def watch_map():
    return {int(w["ric"]): w for w in CFG["watch"] if w.get("on", True) and str(w.get("ric", "")).isdigit()}

# ------------------------------------------------------------------ sounds (8 synthesized tones, WAV, 22.05 kHz)
def _tone(parts, fs=22050):
    """parts: [(seconds, f(t)->sample in -1..1)] -> 16-bit WAV bytes with 5 ms fades at each part's edges."""
    out = []
    for secs, fn in parts:
        n = int(secs * fs); fade = int(0.005 * fs)
        for i in range(n):
            g = min(1.0, i / fade if fade else 1, (n - 1 - i) / fade if fade else 1)
            out.append(int(max(-1, min(1, fn(i / fs))) * g * 26000))
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(fs); w.writeframes(struct.pack("<%dh" % len(out), *out))
    return bio.getvalue()

def _sin(f): return lambda t: math.sin(2 * math.pi * f * t)
def _gap(s): return (s, lambda t: 0.0)

def _chirp(f0, f1, d): return (d, lambda t: math.sin(2 * math.pi * (f0 * t + (f1 - f0) * t * t / (2 * d))))

SOUND_DEFS = [
    lambda: [_chirp(800, 2400, .18), _gap(.06)] * 3,                                                        # 1 Chirp
    lambda: [(.9, _sin(853.2)), (1.4, _sin(960.0))],                                                        # 2 Two-tone sequential
    lambda: [(.14, _sin(1000)), _gap(.09)] * 3,                                                             # 3 Triple beep
    lambda: [(.25, lambda t: (2 * ((t * 420) % 1) - 1) * .8), (.25, lambda t: (2 * ((t * 520) % 1) - 1) * .8)] * 3,   # 4 Klaxon (saw)
    lambda: [_chirp(600, 1400, .6), _chirp(1400, 600, .6)] * 2,                                             # 5 Siren sweep
    lambda: [(.9, lambda t: math.exp(-4 * t) * (math.sin(2 * math.pi * 1320 * t) + .5 * math.sin(2 * math.pi * 2640 * t) + .3 * math.sin(2 * math.pi * 3960 * t)) / 1.8)] * 2,  # 6 Bell
    lambda: [(1.6, lambda t: math.sin(2 * math.pi * (1400 if int(t * 20) % 2 else 1750) * t))],             # 7 Warble
    lambda: [(.08, _sin(2900)), _gap(.06)] * 10,                                                            # 8 Alarm (fast beeps)
]

def sound_path(n): return os.path.join(SOUNDS, "sound-%d.wav" % n)
def make_sounds():
    os.makedirs(SOUNDS, exist_ok=True)
    for i, f in enumerate(SOUND_DEFS, 1):
        if not os.path.exists(sound_path(i)):
            with open(sound_path(i), "wb") as fh: fh.write(_tone(f()))

PLAYING = threading.Lock()
def play_device(n):
    """Play on the uConsole speaker. One at a time, so a burst of pages doesn't stack up sounds."""
    if not PLAYING.acquire(blocking=False): return
    def run():
        try:
            env = dict(os.environ, XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid())
            vol = str(max(0.05, min(1.0, float(CFG.get("volume") or .8))))
            r = subprocess.run(["pw-play", "--volume", vol, sound_path(n)], env=env, capture_output=True, timeout=10)
            if r.returncode: subprocess.run(["aplay", "-q", sound_path(n)], env=env, capture_output=True, timeout=10)
        except Exception: pass
        finally: PLAYING.release()
    spawn(run)

# ------------------------------------------------------------------ recorder
RECENT = {}   # (ric, msg) -> t, for duplicate suppression

def dev_ppm(lease):
    if CFG.get("ppm") not in (None, ""): return int(round(float(CFG["ppm"])))
    if lease.dev["aio"]:                                     # the AIO dongle's measured ppm lives with Looking Glass
        try: return int(round(float(re.search(r"^SDR_PPM=([-\d.]+)", open("/etc/default/sdr-looking-glass").read(), re.M).group(1))))
        except Exception: pass
    return 0

def handle_page(d):
    now = time.time(); d["freq"] = ST["freq"]
    wm = watch_map(); rics = d.get("rics") or [d["ric"]]
    hit = next((wm[r] for r in rics if r in wm), None)
    key = (d["ric"], d["msg"])
    dup = int(now - RECENT.get(key, 0) < float(CFG.get("dedupe_s") or 0))
    RECENT[key] = now
    if len(RECENT) > 5000: RECENT.clear()
    with db() as c:
        cur = c.execute("INSERT INTO pages (t, proto, ric, rics, func, kind, msg, freq, watched, dup) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (now, d["proto"], d["ric"], " ".join(map(str, rics)), d.get("func"), d["kind"], d["msg"], d["freq"], int(bool(hit)), dup))
        pid = cur.lastrowid
    with LOCK:
        ST["pages"] += 1; ST["last_page"] = now; ST["by_proto"][d["proto"]] = ST["by_proto"].get(d["proto"], 0) + 1
    row = dict(d, id=pid, t=now, watched=int(bool(hit)), dup=dup, rics=" ".join(map(str, rics)))
    if hit:
        row.update(label=hit.get("label", ""), sound=int(hit.get("sound") or 1), color=hit.get("color"))
        if not dup and CFG.get("sound_device"): play_device(row["sound"])
    app.push({"t": "page", "page": row, "alert": bool(hit and not dup)})

def _reader(p2):
    for raw in p2.stdout:
        line = raw.decode(errors="replace")
        with LOCK: ST["lines"] += 1
        d = co_decode.parse_multimon(line)
        if d and d.get("type") in ("POCSAG", "FLEX"):
            if d.get("corrupt"):
                with LOCK: ST["corrupt"] = ST.get("corrupt", 0) + 1
                continue
            try: handle_page(d)
            except Exception as e: app.push({"t": "log", "msg": "store error: %s" % e})

def _stderr(p, tag):
    for raw in p.stderr:
        s = raw.decode(errors="replace").strip()
        if s and re.search(r"fail|error|no supported|usb_claim|lost", s, re.I):
            with LOCK: ST["error"] = "%s: %s" % (tag, s[:160])
            app.push({"t": "log", "msg": ST["error"]})

def _pipeline(lease):
    """Run rtl_fm | multimon-ng until it exits (stop / settings reload / rtl_fm died)."""
    freq = float(CFG["freq"])
    rtl = ["rtl_fm", "-d", str(lease.index), "-f", "%.5fM" % freq, "-M", "fm", "-s", "22050", "-E", "dc", "-F", "9", "-p", str(dev_ppm(lease))]
    if str(CFG.get("gain", "")).strip().lower() not in ("", "auto"): rtl += ["-g", str(CFG["gain"])]
    if int(CFG.get("squelch") or 0) > 0: rtl += ["-l", str(int(CFG["squelch"]))]
    rtl += ["-"]
    mm = ["multimon-ng", "-t", "raw", "-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400", "-a", "FLEX", "-f", "auto"]
    if CFG.get("prune"): mm += ["-u"]
    if CFG.get("invert"): mm += ["-i"]
    mm += ["-"]
    p1 = subprocess.Popen(rtl, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p2 = subprocess.Popen(mm, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE); p1.stdout.close()
    with LOCK:
        ST.update(running=True, error=None, device=lease.describe(), borrowed=lease.borrowed, since=time.time(), freq=freq, procs=(p1, p2))
    app.push({"t": "status"})
    spawn(_stderr, p1, "rtl_fm"); spawn(_stderr, p2, "multimon-ng")
    _reader(p2)
    for p in (p1, p2):
        try: p.terminate(); p.wait(3)
        except Exception:
            try: p.kill()
            except Exception: pass
    with LOCK: ST.update(running=False, procs=None)

def _loop(gen):
    """One radio lease for the whole recording session; the pipeline restarts inside it (settings change, crash)."""
    live = lambda: ST["want"] and ST["gen"] == gen
    back = 2; lease = None
    try:
        while live():
            if lease is None or ST.get("release"):
                if lease: lease.release(); lease = None
                ST["release"] = False
                lease, why = co_radio.lease("Pager Recorder", prefer=CFG.get("device"))
                if not lease:
                    with LOCK: ST["error"] = why
                    app.push({"t": "status"}); time.sleep(back); back = min(back * 2, 30); continue
            t0 = time.time(); _pipeline(lease)
            if not live(): break
            if ST.pop("reload", False): continue                    # settings change: rerun at once, keep the dongle
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

def reload(new_device=False):
    """Apply radio settings: restart the pipeline inside the running session (no Looking Glass churn)."""
    if not ST["want"]: return
    ST["reload"] = True
    if new_device: ST["release"] = True
    _kill()

def prune_loop():
    while True:
        try:
            with db() as c:
                n = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
                if n > MAX_ROWS: c.execute("DELETE FROM pages WHERE id IN (SELECT id FROM pages ORDER BY id LIMIT ?)", (n - MAX_ROWS,))
        except Exception: pass
        time.sleep(3600)

# ------------------------------------------------------------------ API
def status():
    with LOCK:
        s = {k: ST.get(k) for k in ("want", "running", "error", "device", "borrowed", "since", "freq", "lines", "pages", "by_proto", "last_page", "corrupt")}
    s["clients"] = app.clients(); s["radios"] = co_radio.status(); return s

@app.get("/api/state")
def api_state(q):
    with db() as c:
        tot = c.execute("SELECT COUNT(*), SUM(watched) FROM pages").fetchone()
        per = {r[0]: {"n": r[1], "last": r[2]} for r in c.execute("SELECT ric, COUNT(*), MAX(t) FROM pages WHERE watched=1 GROUP BY ric")}
    cfg = dict(CFG, watch=[dict(w) for w in CFG["watch"]])
    for w in cfg["watch"]: w.update(per.get(int(w["ric"]), {"n": 0, "last": None}) if str(w.get("ric", "")).isdigit() else {})
    return {"status": status(), "cfg": cfg, "sounds": SOUND_NAMES, "stored": tot[0], "stored_watched": tot[1] or 0}

@app.get("/api/pages")
def api_pages(q):
    lim = max(1, min(2000, int(q.get("limit") or 300)))
    w, a = [], []
    if q.get("all") != "1": w.append("watched=1")
    if q.get("ric"): w.append("(ric=? OR (' '||rics||' ') LIKE ?)"); a += [int(q["ric"]), "% " + q["ric"] + " %"]
    if q.get("q"): w.append("msg LIKE ?"); a.append("%" + q["q"] + "%")
    if q.get("nodup") == "1": w.append("dup=0")
    if q.get("notone") == "1": w.append("NOT (kind='tone' AND watched=0)")   # watched tone pages still show
    if q.get("before"): w.append("id<?"); a.append(int(q["before"]))
    sql = "SELECT * FROM pages %s ORDER BY id DESC LIMIT %d" % (("WHERE " + " AND ".join(w)) if w else "", lim)
    with db() as c: rows = [dict(r) for r in c.execute(sql, a)]
    wm = {int(x["ric"]): x for x in CFG["watch"] if str(x.get("ric", "")).isdigit()}
    for r in rows:
        x = next((wm[int(k)] for k in (r.get("rics") or str(r["ric"])).split() if int(k) in wm), None)
        if x: r.update(label=x.get("label", ""), sound=int(x.get("sound") or 1), color=x.get("color"))
    return rows

@app.get("/api/export.csv")
def api_export(q):
    import csv
    out = io.StringIO(); wr = csv.writer(out)
    wr.writerow(["time", "proto", "ric", "rics", "function", "kind", "message", "mhz", "watched", "duplicate"])
    with db() as c:
        for r in c.execute("SELECT * FROM pages %s ORDER BY id" % ("" if q.get("all") == "1" else "WHERE watched=1")):
            wr.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["t"])), r["proto"], r["ric"], r["rics"], r["func"],
                         r["kind"], r["msg"], r["freq"], r["watched"], r["dup"]])
    return (out.getvalue(), "text/csv", {"Content-Disposition": "attachment; filename=pages-%s.csv" % time.strftime("%Y%m%d-%H%M")})

@app.get("/sound/")
def api_sound(q):
    n = int(re.sub(r"\D", "", q.get("_rest", "")) or 1)
    if not 1 <= n <= 8: return 404, {"ok": False}
    with open(sound_path(n), "rb") as f: return (f.read(), "audio/wav")

@app.post("/api/start")
def api_start(q, b):
    if b.get("freq"): CFG["freq"] = round(float(b["freq"]), 5); save_cfg()
    if ST["want"]: reload()
    else: start()
    return {"ok": True}

@app.post("/api/stop")
def api_stop(q, b): stop(); return {"ok": True}

SETTABLE = {"freq": float, "gain": str, "ppm": lambda v: None if v in (None, "") else float(v), "squelch": int, "prune": bool,
            "invert": bool, "debug": bool, "sound_device": bool, "sound_browser": bool, "volume": float, "dedupe_s": int,
            "record_on_boot": bool, "device": lambda v: v or None}

@app.post("/api/settings")
def api_settings(q, b):
    radio = newdev = False
    with LOCK:
        for k, f in SETTABLE.items():
            if k in b:
                v = f(b[k])
                if v != CFG.get(k) and k in ("freq", "gain", "ppm", "squelch", "prune", "invert", "device"): radio = True
                if v != CFG.get(k) and k == "device": newdev = True
                CFG[k] = v
    save_cfg()
    if radio: reload(newdev)
    return {"ok": True, "restarted": radio and ST["want"]}

@app.post("/api/watch")
def api_watch(q, b):
    ric = str(b.get("ric", "")).strip()
    if not ric.isdigit(): return 400, {"ok": False, "err": "RIC / capcode must be a number"}
    with LOCK:
        w = next((x for x in CFG["watch"] if str(x["ric"]) == ric), None)
        if not w: w = {"ric": int(ric), "label": "", "sound": 1, "color": "#ff7a1a", "on": True}; CFG["watch"].append(w)
        for k in ("label", "color"):
            if k in b: w[k] = str(b[k])[:60]
        if "sound" in b: w["sound"] = max(1, min(8, int(b["sound"])))
        if "on" in b: w["on"] = bool(b["on"])
        CFG["watch"].sort(key=lambda x: int(x["ric"]))
    save_cfg()
    with db() as c: c.execute("UPDATE pages SET watched=1 WHERE ric=?", (int(ric),))   # earlier pages to it now show as watched
    return {"ok": True}

@app.post("/api/watch/delete")
def api_watch_del(q, b):
    with LOCK: CFG["watch"] = [x for x in CFG["watch"] if str(x["ric"]) != str(b.get("ric"))]
    save_cfg()
    with db() as c: c.execute("UPDATE pages SET watched=0 WHERE ric=?", (int(b.get("ric") or -1),))
    return {"ok": True}

@app.post("/api/sound")
def api_play(q, b): play_device(max(1, min(8, int(b.get("n") or 1)))); return {"ok": True}

@app.post("/api/clear")
def api_clear(q, b):
    with db() as c:
        if b.get("scope") == "unwatched": n = c.execute("DELETE FROM pages WHERE watched=0").rowcount
        else: n = c.execute("DELETE FROM pages").rowcount
    return {"ok": True, "deleted": n}

if __name__ == "__main__":
    make_sounds(); spawn(prune_loop)
    if CFG.get("record_on_boot"): start()
    app.serve()
