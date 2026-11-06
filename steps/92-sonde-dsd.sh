# Radiosondes (radiosonde_auto_rx, :5000) + digital voice (dsd-fme). Both take the RTL through co_radio.
# auto_rx lives in ~/radiosonde_auto_rx (its own git checkout + venv); station.cfg is made once from the example
# (US band 400.05-405.95, gpsd location, gain 40, web on all interfaces, SondeHub upload + LAN UDP OFF) and is
# then yours: re-runs never overwrite it. Turning on sondehub_enabled shares your station position publicly.
sudo apt-get install -y -qq build-essential libfftw3-dev python3-venv python3-numpy git
R=$HOME/radiosonde_auto_rx
[ -d "$R/.git" ] || git clone -q --depth 1 https://github.com/projecthorus/radiosonde_auto_rx "$R"
if [ ! -x "$R/auto_rx/rs41mod" ]; then (cd "$R/auto_rx" && ./build.sh > /tmp/sonde-build.log 2>&1) || { tail -20 /tmp/sonde-build.log; exit 1; }; echo "  auto_rx decoders built"; fi
if [ ! -x "$R/venv/bin/python" ]; then
  python3 -m venv --system-site-packages "$R/venv" && "$R/venv/bin/pip" install -q -r "$R/auto_rx/requirements.txt"; echo "  auto_rx venv ready"
fi
C=$R/auto_rx/station.cfg
if [ ! -f "$C" ]; then
  python3 - "$R/auto_rx/station.cfg.example" "$C" <<'PY'
import re, sys, json, socket
s = open(sys.argv[1]).read()
lat = lon = 0.0
try:                                                   # one gpsd fix for the static fallback location
    k = socket.create_connection(("127.0.0.1", 2947), timeout=2); k.sendall(b'?WATCH={"enable":true,"json":true}\n'); k.settimeout(3)
    buf = b""
    for _ in range(40):
        buf += k.recv(4096)
        for ln in buf.split(b"\n"):
            try: m = json.loads(ln)
            except Exception: continue
            if m.get("class") == "TPV" and m.get("mode", 0) >= 2: lat, lon = round(m["lat"], 3), round(m["lon"], 3); raise StopIteration
except Exception: pass
def put(key, val, after=None):
    global s
    start = s.index(after) if after else 0
    m = re.compile(r"(?m)^%s\s*=.*$" % re.escape(key)).search(s, start)
    s = s[:m.start()] + "%s = %s" % (key, val) + s[m.end():]
for k, v, a in [("gain", "40", "[sdr_1]"), ("min_freq", "400.05", None), ("max_freq", "405.95", None),
                ("station_lat", lat, None), ("station_lon", lon, None), ("gpsd_enabled", "True", None),
                ("sondehub_enabled", "False", None), ("payload_summary_enabled", "False", None), ("web_host", "0.0.0.0", None)]:
    put(k, v, a)
open(sys.argv[2], "w").write(s)
print("  made station.cfg (location %s, %s; SondeHub upload off)" % (lat, lon))
PY
fi
put /opt/clockwork-orange/sonde/sonde.py || true
put /usr/local/bin/co-sonde 0755 || true
put /etc/systemd/system/co-sonde.service && sudo systemctl daemon-reload || true
# dsd-fme (DMR / NXDN / dPMR / YSF / P25 conventional + M17, voice via mbelib) into /usr/local/bin. Not in Debian.
if [ ! -x /usr/local/bin/dsd-fme ]; then
  sudo apt-get install -y -qq libsndfile1-dev libfftw3-dev libpulse-dev libncurses-dev libcodec2-dev librtlsdr-dev cmake build-essential git
  t=$(mktemp -d)
  if ! /sbin/ldconfig -p | grep -q 'libmbe.so'; then
    git clone -q --depth 1 https://github.com/szechyjs/mbelib "$t/mbelib"
    { cmake -S "$t/mbelib" -B "$t/mbelib/build" && make -C "$t/mbelib/build" -j3 && sudo make -C "$t/mbelib/build" install; } > "$t/mbe.log" 2>&1 || { tail -20 "$t/mbe.log"; exit 1; }
    sudo ldconfig
  fi
  git clone -q --depth 1 https://github.com/lwvmobile/dsd-fme "$t/dsd"
  { cmake -S "$t/dsd" -B "$t/dsd/build" && make -C "$t/dsd/build" -j3; } > "$t/build.log" 2>&1 || { tail -30 "$t/build.log"; exit 1; }
  sudo install -m 0755 "$t/dsd/build/dsd-fme" /usr/local/bin/dsd-fme; rm -rf "$t"; echo "  dsd-fme built"
fi
put /usr/local/bin/co-dsd 0755 || true
