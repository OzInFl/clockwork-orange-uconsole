# Keyboard/trackball: flash j1n6 QMK (fixes the trackball) if the keyboard still runs stock firmware.
QMK_VER=v2.1.1
# trackball feel (desktop side, built-in ball only): default to the flat 'smooth' preset once
put /usr/local/bin/co-trackball 0755 || true
co-trackball status | grep -q 'not configured' && co-trackball smooth || co-trackball status
# X/Y/A/B buttons -> F13-F16 (hwdb) -> labwc keybinds (fullscreen / LG uConsole mode / LG audio)
if put /etc/udev/hwdb.d/70-uconsole-xyba.hwdb; then sudo systemd-hwdb update && sudo udevadm trigger -s input; fi
put /usr/local/bin/co-lg-cmd 0755 || true
put /usr/local/bin/co-buttons 0755 || true
co-buttons install
D="$HOME/uconsole-kb/uconsole_keyboard_flash"
if lsusb | grep -q 'feed:0000'; then echo "  keyboard already on QMK - skipping"; exit 0; fi
lsusb | grep -q '1eaf:0024' || { echo "  stock keyboard (1eaf:0024) not found - skipping"; exit 0; }
mkdir -p "$HOME/uconsole-kb" && cd "$HOME/uconsole-kb"
[ -d "$D" ] || { wget -q https://github.com/clockworkpi/uConsole/raw/master/Bin/uconsole_keyboard_flash.tar.gz; tar zxf uconsole_keyboard_flash.tar.gz; }
cd "$D"; sed -i 's/} 750$/} 1500/' maple_upload   # stock 750ms reset delay is too short
[ -f clockworkpi_uconsole_default.bin ] || wget -q "https://github.com/j1n6/qmk-uconsole/releases/download/$QMK_VER/clockworkpi_uconsole_default.bin"
[ "${CO_FLASH_KB:-0}" = 1 ] || { echo "  staged in $D; re-run with CO_FLASH_KB=1 to flash (keep SSH/USB mouse handy)"; exit 0; }
sudo ./maple_upload ttyACM0 2 1EAF:0003 clockworkpi_uconsole_default.bin || true
sleep 4; lsusb | grep -q 'feed:0000' && echo "  QMK flashed OK" || { echo "!! keyboard not back on QMK - see CLAUDE.md recovery"; exit 1; }
