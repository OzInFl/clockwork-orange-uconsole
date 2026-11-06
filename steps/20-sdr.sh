# AIO V2 rails: power SDR + GPS at boot (on CM4 their GPIOs end low after boot), keep the DVB driver off the RTL.
put /usr/local/bin/co-rail 0755 || true
put "/etc/systemd/system/aiov2-rail@.service" && sudo systemctl daemon-reload
if [ -f /etc/systemd/system/aiov2-sdr-power.service ]; then   # migrate from the first single-rail unit
  sudo systemctl disable aiov2-sdr-power.service; sudo rm /etc/systemd/system/aiov2-sdr-power.service; sudo systemctl daemon-reload
fi
sudo systemctl enable --now aiov2-rail@sdr.service aiov2-rail@gps.service
if put /etc/modprobe.d/blacklist-rtlsdr.conf; then echo "  DVB blacklist new: reboot for it to take full effect"; fi
sudo usermod -aG plugdev,dialout "$USER"
sleep 2; rtl_test -t 2>&1 | grep -E "Found|tuner" || { echo "!! RTL-SDR not found (co-rail sdr status / lsusb for 0bda:2838)"; exit 1; }
