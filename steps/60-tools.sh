# RF / field toolset. SDR apps are launched through co-sdr-run (hub/menu) so they don't fight Looking Glass.
sudo apt-get install -y -qq gqrx-sdr cubicsdr inspectrum rtl-433 readsb multimon-ng direwolf \
  gpsd gpsd-clients soapysdr-tools soapysdr-module-rtlsdr minicom tmux htop btop nmap
# SDR++ (nightly .deb for trixie/arm64; not in Debian)
if ! command -v sdrpp >/dev/null; then
  t=$(mktemp -d); wget -q -O "$t/sdrpp.deb" https://github.com/AlexandreRouma/SDRPlusPlus/releases/download/nightly/sdrpp_debian_trixie_aarch64.deb
  sudo apt-get install -y -qq "$t/sdrpp.deb"; rm -rf "$t"
fi
# readsb's package enables a service that grabs the RTL at boot - we launch it on demand instead.
sudo systemctl disable --now readsb.service 2>/dev/null || true
# Debian's readsb is built without RTL-SDR support ("SDR type 'rtlsdr' not recognized"), so build wiedehopf's
# with RTLSDR=yes into /usr/local/bin (ahead of /usr/bin in PATH). ~35 s on the CM4.
if [ ! -x /usr/local/bin/readsb ]; then
  sudo apt-get install -y -qq librtlsdr-dev libncurses-dev zlib1g-dev libzstd-dev pkg-config build-essential git
  t=$(mktemp -d); git clone -q --depth 1 https://github.com/wiedehopf/readsb "$t/readsb"
  make -C "$t/readsb" -j3 RTLSDR=yes > "$t/build.log" 2>&1 || { tail -20 "$t/build.log"; exit 1; }
  sudo install -m 0755 "$t/readsb/readsb" /usr/local/bin/readsb; rm -rf "$t"; echo "  readsb built with RTL-SDR support"
fi
# AIS-catcher (AIS ships, 161.975/162.025 MHz; JSON on stdout + its own web map). Not in Debian. A few minutes on the CM4.
if [ ! -x /usr/local/bin/AIS-catcher ]; then
  sudo apt-get install -y -qq librtlsdr-dev libzmq3-dev libsqlite3-dev zlib1g-dev libssl-dev pkg-config cmake build-essential git
  t=$(mktemp -d); git clone -q --depth 1 https://github.com/jvde-github/AIS-catcher "$t/ais"
  { cmake -S "$t/ais" -B "$t/ais/build" -DCMAKE_BUILD_TYPE=Release && make -C "$t/ais/build" -j3; } > "$t/build.log" 2>&1 || { tail -20 "$t/build.log"; exit 1; }
  sudo install -m 0755 "$t/ais/build/AIS-catcher" /usr/local/bin/AIS-catcher; rm -rf "$t"; echo "  AIS-catcher built"
fi
# acarsdec + libacars (ACARS aircraft datalink, 129-131.55 MHz; libacars decodes the message payloads). Not in Debian.
if [ ! -x /usr/local/bin/acarsdec ]; then
  sudo apt-get install -y -qq librtlsdr-dev libxml2-dev libjansson-dev zlib1g-dev libsndfile1-dev cmake build-essential git
  t=$(mktemp -d)
  if ! /sbin/ldconfig -p | grep -q libacars-2; then
    git clone -q --depth 1 https://github.com/szpajder/libacars "$t/libacars"
    { cmake -S "$t/libacars" -B "$t/libacars/build" && make -C "$t/libacars/build" -j3 && sudo make -C "$t/libacars/build" install; } > "$t/la.log" 2>&1 || { tail -20 "$t/la.log"; exit 1; }
    sudo ldconfig
  fi
  git clone -q --depth 1 https://github.com/TLeconte/acarsdec "$t/acarsdec"
  { cmake -S "$t/acarsdec" -B "$t/acarsdec/build" -Drtl=ON && make -C "$t/acarsdec/build" -j3; } > "$t/build.log" 2>&1 || { tail -20 "$t/build.log"; exit 1; }
  sudo install -m 0755 "$t/acarsdec/build/acarsdec" /usr/local/bin/acarsdec; rm -rf "$t"; echo "  acarsdec built"
fi
# dumpvdl2 (VDL Mode 2 aircraft datalink, 136.65-136.975 MHz; ACARS-over-AVLC + logons; uses the libacars above). Not in Debian.
if [ ! -x /usr/local/bin/dumpvdl2 ]; then
  sudo apt-get install -y -qq libglib2.0-dev librtlsdr-dev libzmq3-dev cmake build-essential git
  t=$(mktemp -d); git clone -q --depth 1 https://github.com/szpajder/dumpvdl2 "$t/vdl"
  { cmake -S "$t/vdl" -B "$t/vdl/build" -DRTLSDR=TRUE && make -C "$t/vdl/build" -j3; } > "$t/build.log" 2>&1 || { tail -20 "$t/build.log"; exit 1; }
  sudo install -m 0755 "$t/vdl/build/src/dumpvdl2" /usr/local/bin/dumpvdl2; rm -rf "$t"; echo "  dumpvdl2 built"
fi
# GPS: gpsd on the AIO UART; the serial login console on the same port would eat the NMEA.
sudo systemctl disable --now serial-getty@ttyS0.service 2>/dev/null || true
sudo systemctl mask serial-getty@ttyS0.service
sudo chgrp dialout /dev/ttyS0 && sudo chmod 660 /dev/ttyS0
put /etc/default/gpsd || true
put /usr/local/bin/co-gps-init 0755 || true
put /etc/systemd/system/gpsd.service.d/co-gps-init.conf && sudo systemctl daemon-reload || true
sudo systemctl reset-failed gpsd.service 2>/dev/null || true   # clear a start-limit-hit from a bad options file
sudo systemctl restart gpsd.socket gpsd.service || true
sudo systemctl enable gpsd.socket gpsd.service
for f in co-sdr-run co-aprs; do put /usr/local/bin/$f 0755 || true; done
echo "  tools: $(for c in sdrpp gqrx CubicSDR inspectrum rtl_433 readsb direwolf cgps; do command -v $c >/dev/null && printf '%s ' $c || printf '!%s ' $c; done)"
