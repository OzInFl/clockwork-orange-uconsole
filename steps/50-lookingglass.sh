# SDR Looking Glass (uConsole edition) as a service on :8080. Cloned from GitHub (CO_LG_REPO) and updated with
# git pull on re-runs; a copy pushed by sync-lookingglass.ps1 (no .git) is left as it is.
LG="$HOME/sdr-looking-glass"
if [ -d "$LG/.git" ]; then git -C "$LG" pull -q --ff-only || echo "  (Looking Glass: git pull failed, keeping the current version)"
elif [ ! -f "$LG/dashboard/app.py" ]; then
  [ -e "$LG" ] && mv "$LG" "$LG.old-$(date +%s)"
  git clone -q "${CO_LG_REPO:-https://github.com/OzInFl/sdr-looking-glass-uconsole}" "$LG"
fi
[ -f "$HOME/op25/op25/op25/gr-op25_repeater/apps/multi_rx.py" ] || echo "  (OP25 not built yet - run step 40-op25; the dashboard starts but can't decode)"
# per-unit tuning lives here, not in the repo (SDR_PPM = this dongle's measured ppm)
[ -f /etc/default/sdr-looking-glass ] || printf 'SDR_PPM=0\n' | sudo tee /etc/default/sdr-looking-glass >/dev/null
# pin OP25 to the AIO V2's own RTL by serial, so a second USB RTL (pager/spy apps) can't take index 0
put /opt/clockwork-orange/lib/co_radio.py || true
if ! grep -q '^SDR_ARGS=' /etc/default/sdr-looking-glass; then
  sn=$(python3 /opt/clockwork-orange/lib/co_radio.py aio-serial 2>/dev/null)
  [ -n "$sn" ] && echo "SDR_ARGS=rtl=$sn" | sudo tee -a /etc/default/sdr-looking-glass >/dev/null && echo "  OP25 pinned to AIO RTL SN $sn"
fi
put /etc/systemd/system/sdr-looking-glass.service && sudo systemctl daemon-reload
sudo systemctl enable sdr-looking-glass.service
sudo systemctl restart sdr-looking-glass.service
sleep 4; curl -fs -o /dev/null http://localhost:8080/ && echo "  dashboard up: http://$(hostname -I | cut -d' ' -f1):8080" || { journalctl -u sdr-looking-glass -n 20 --no-pager; exit 1; }
