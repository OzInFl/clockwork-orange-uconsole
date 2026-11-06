#!/usr/bin/env python3
"""co_radio - share the uConsole's RTL-SDRs between Clockwork Orange apps.

The AIO V2 has one RTL ("AIO_V2 Ext"), normally held by SDR Looking Glass (OP25). A second USB RTL is
optional. lease(owner) hands an app a free dongle: a second RTL if one is free, otherwise the AIO one,
pausing Looking Glass while it's borrowed and starting it again on release. Each dongle has a flock in
/tmp/co-radio, so two apps never open the same one; the kernel drops the lock if an app dies, and
recover() restarts Looking Glass if a dead app had borrowed the AIO RTL.

  co_radio.py list          dongles + who holds them (JSON)
  co_radio.py aio-serial    the AIO RTL's serial (used by step 50 to pin OP25 to it)
"""
import ctypes, fcntl, json, os, subprocess, sys, time

LG = "sdr-looking-glass"
LOCKDIR = "/tmp/co-radio"
BORROW = os.path.join(LOCKDIR, "lg-borrowed.json")     # {"owner", "pid", "serial", "t"} while an app has the AIO RTL

def _lib():
    for n in ("librtlsdr.so.2", "librtlsdr.so.0", "librtlsdr.so"):
        try: return ctypes.CDLL(n)
        except OSError: pass
    return None

def devices():
    """[{index, maker, product, serial, aio}] in librtlsdr index order (what rtl_fm/-d, rtl_433 -d, readsb --device use)."""
    lib = _lib(); out = []
    if not lib: return out
    for i in range(lib.rtlsdr_get_device_count()):
        b = [ctypes.create_string_buffer(256) for _ in range(3)]
        lib.rtlsdr_get_device_usb_strings(i, *b)
        m, p, s = (x.value.decode(errors="replace") for x in b)
        out.append({"index": i, "maker": m, "product": p, "serial": s, "aio": "AIO" in p.upper()})
    return out

def _sh(*a, timeout=25):
    try: return subprocess.run(a, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception: return ""

def lg_active(): return _sh("systemctl", "is-active", LG) == "active"

def _alive(pid):
    try: os.kill(pid, 0); return True
    except Exception: return False

def _key(d): return "rtl-%s-%d" % (d["serial"] or "x", d["index"])

def holders():
    """{lock key: owner} for dongles currently leased by a Clockwork Orange app."""
    out = {}
    for d in devices():
        p = os.path.join(LOCKDIR, _key(d) + ".lock")
        try:
            with open(p) as f:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(f, fcntl.LOCK_UN)
        except FileNotFoundError: continue
        except BlockingIOError:
            try: out[_key(d)] = open(p + ".owner").read().strip()
            except Exception: out[_key(d)] = "?"
    return out

def recover():
    """Restart Looking Glass if the app that borrowed the AIO RTL is gone (crash / kill -9)."""
    try: b = json.load(open(BORROW))
    except Exception: return
    if not _alive(b.get("pid", 0)):
        try: os.remove(BORROW)
        except Exception: pass
        if not lg_active(): _sh("sudo", "systemctl", "start", LG)

class Lease:
    def __init__(self, dev, fh, owner, borrowed):
        self.dev, self.fh, self.owner, self.borrowed = dev, fh, owner, borrowed
        self.index, self.serial = dev["index"], dev["serial"]

    def describe(self):
        d = self.dev
        return "%s %s (SN %s)%s" % (d["maker"], d["product"], d["serial"],
                                     " - borrowed from Looking Glass" if self.borrowed else "")

    def release(self):
        if self.fh is None: return
        try: fcntl.flock(self.fh, fcntl.LOCK_UN); self.fh.close()
        except Exception: pass
        self.fh = None
        if self.borrowed:
            try: os.remove(BORROW)
            except Exception: pass
            _sh("sudo", "systemctl", "start", LG)
            self.borrowed = False

    def __enter__(self): return self
    def __exit__(self, *a): self.release()

def lease(owner, allow_borrow=True, prefer=None):
    """A free dongle for <owner>, or (None, reason). prefer = serial to try first (e.g. the user's choice)."""
    os.makedirs(LOCKDIR, exist_ok=True); recover()
    devs = devices()
    if not devs: return None, "no RTL-SDR found (is the SDR rail on? co-rail sdr on)"
    devs.sort(key=lambda d: (d["serial"] != prefer if prefer else 0, d["aio"], d["index"]))   # second RTL before the AIO one
    why = []
    for d in devs:
        p = os.path.join(LOCKDIR, _key(d) + ".lock")
        fh = open(p, "a+")
        try: fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            try: why.append("%s is in use by %s" % (d["product"], open(p + ".owner").read().strip()))
            except Exception: why.append("%s is in use" % d["product"])
            continue
        borrowed = False
        if d["aio"] and lg_active():
            if not allow_borrow:
                fh.close(); why.append("%s is held by Looking Glass" % d["product"]); continue
            json.dump({"owner": owner, "pid": os.getpid(), "serial": d["serial"], "t": time.time()}, open(BORROW, "w"))
            _sh("sudo", "systemctl", "stop", LG); time.sleep(1.0); borrowed = True
        with open(p + ".owner", "w") as f: f.write(owner)
        return Lease(d, fh, owner, borrowed), None
    return None, "; ".join(why) or "no free RTL-SDR"

def status():
    h = holders()
    return [dict(d, holder=h.get(_key(d)) or ("Looking Glass" if d["aio"] and lg_active() else None)) for d in devices()]

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "aio-serial":
        d = next((d for d in devices() if d["aio"]), None)
        if d and d["serial"]: print(d["serial"])
        else: sys.exit(1)
    else:
        print(json.dumps(status(), indent=1))
