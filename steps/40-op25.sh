# OP25 (boatbod, GNU Radio 3.10) built from source; repo at ~/op25/op25, apps at ~/op25/op25/op25/gr-op25_repeater/apps
# (sdr-looking-glass.service points OP25_APPS there).
# boatbod's install.sh can't enable deb-src on trixie (deb822 sources), so install deps directly.
sudo apt-get install -y -qq gnuradio gnuradio-dev gr-osmosdr librtlsdr-dev libuhd-dev libhackrf-dev liborc-dev \
  cmake git build-essential pkg-config doxygen clang-format python3-pybind11 python3-numpy python3-waitress \
  python3-requests gnuplot-x11 libsndfile1-dev libspdlog-dev libitpp-dev libpcap-dev
mkdir -p "$HOME/op25"; cd "$HOME/op25"
[ -d op25 ] || git clone -q https://github.com/boatbod/op25.git
cd op25; echo /usr/bin/python3 > op25/gr-op25_repeater/apps/op25_python
python3 "$CO_DIR/tools/op25_rssi_patch.py" op25/gr-op25_repeater/apps   # RSSI for Looking Glass (python-only, idempotent)
if [ -f build/.co_built ] && [ "$(cat build/.co_built)" = "$(git rev-parse HEAD)" ]; then echo "  OP25 already built at $(git rev-parse --short HEAD)"; exit 0; fi
rm -rf build && mkdir build && cd build
cmake ../ > cmake.log 2>&1
make -j3 > make.log 2>&1 || { tail -30 make.log; exit 1; }   # -j3: leave a core for the desktop; -j4 can OOM
sudo make install > install.log 2>&1 && sudo ldconfig
git rev-parse HEAD > .co_built; echo "  OP25 built"
