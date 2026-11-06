#!/usr/bin/env python3
"""Clockwork Orange Radiosondes - runs radiosonde_auto_rx (~/radiosonde_auto_rx, web UI + map on :5000) on a leased RTL.

Takes a co_radio lease (second USB RTL first, else borrows the AIO one from Looking Glass, which comes back when
this exits), writes the leased dongle's serial into station.cfg [sdr_1] device_idx, and runs auto_rx until
stopped (SIGTERM from `systemctl stop co-sonde`). Receive only; SondeHub upload is off unless you turn it on.
"""
import os, re, signal, subprocess, sys
sys.path.insert(0, "/opt/clockwork-orange/lib")
import co_radio

RX = os.path.expanduser("~/radiosonde_auto_rx/auto_rx")
CFG = os.path.join(RX, "station.cfg")

def main():
    co_radio.recover()
    lease, why = co_radio.lease("Radiosondes")
    if not lease:
        print("no radio:", why, flush=True); sys.exit(1)
    print("radio:", lease.describe(), flush=True)
    s = open(CFG).read()
    sec = s.index("[sdr_1]"); end = s.index("\n[", sec + 1)
    blk = re.sub(r"(?m)^device_idx\s*=.*$", "device_idx = %s" % (lease.serial or lease.index), s[sec:end], count=1)
    open(CFG, "w").write(s[:sec] + blk + s[end:])
    p = subprocess.Popen([os.path.join(RX, "..", "venv", "bin", "python"), "auto_rx.py", "-c", CFG], cwd=RX)
    def stop(*_):
        try: p.send_signal(signal.SIGINT)                 # auto_rx shuts its decoders down cleanly on Ctrl-C
        except Exception: pass
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        try: p.wait()
        except KeyboardInterrupt: stop()
        try: p.wait(timeout=20)
        except subprocess.TimeoutExpired: p.kill()
    finally:
        lease.release()

if __name__ == "__main__":
    main()
