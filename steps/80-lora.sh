# LoRa (AIO V2 SX1262 on SPI1): Meshtastic (meshtasticd service + web UI :9443) and MeshCore
# (cwill747/meshcore-uconsole GTK client). One radio: MeshCore launches via co-lora-run, which pauses meshtasticd.
C=/boot/firmware/config.txt
if ! grep -q '^dtoverlay=spi1-1cs' $C; then
  sudo cp $C $C.bak.$(date +%F); sudo sed -i '/^\[all\]/a dtoverlay=spi1-1cs' $C; echo "  !! added SPI1 overlay - reboot, then re-run this step"; exit 0
fi
[ -e /dev/spidev1.0 ] || { echo "!! /dev/spidev1.0 missing - reboot needed"; exit 1; }
sudo systemctl enable --now aiov2-rail@lora.service
# Meshtastic from the official OBS repo (beta = Meshtastic's recommended channel for meshtasticd)
if [ ! -f /etc/apt/sources.list.d/meshtastic.list ]; then
  curl -fsSL https://download.opensuse.org/repositories/network:/Meshtastic:/beta/Debian_13/Release.key | gpg --dearmor | sudo tee /usr/share/keyrings/meshtastic.gpg >/dev/null
  echo 'deb [signed-by=/usr/share/keyrings/meshtastic.gpg] https://download.opensuse.org/repositories/network:/Meshtastic:/beta/Debian_13/ /' | sudo tee /etc/apt/sources.list.d/meshtastic.list >/dev/null
fi
# MeshCore uConsole client
if [ ! -f /etc/apt/sources.list.d/meshcore.list ]; then
  curl -fsSL https://cwill747.github.io/meshcore-uconsole/KEY.gpg | sudo gpg --dearmor -o /usr/share/keyrings/meshcore.gpg
  echo 'deb [signed-by=/usr/share/keyrings/meshcore.gpg arch=arm64] https://cwill747.github.io/meshcore-uconsole trixie main' | sudo tee /etc/apt/sources.list.d/meshcore.list >/dev/null
fi
sudo apt-get update -qq
sudo apt-get install -y -qq -o Dpkg::Options::=--force-confold meshtasticd meshcore-uconsole pipx
# A LoRa tool started through co-lora-run (MeshCore) owns the radio: then don't (re)start meshtasticd under it;
# co-lora-run resumes meshtasticd itself when the tool exits.
lora_busy() { pgrep -f '^/bin/bash /usr/local/bin/co-lora-run' >/dev/null; }
put /etc/meshtasticd/config.d/uconsole-aiov2.yaml && { lora_busy || sudo systemctl restart meshtasticd; } || true
put /etc/default/meshcore-uconsole || true
# MeshCore Console is laid out for a ~11pt base font; the desktop Sans 20 pushed it past 1280px. Scoped to its window class.
install -D -m 0644 "$CO_DIR/files/home/pi/.config/gtk-4.0/gtk.css" "$HOME/.config/gtk-4.0/gtk.css"
put /usr/local/bin/co-lora-run 0755 || true
put /etc/default/gpsd && sudo systemctl restart gpsd || true
sudo systemctl enable meshtasticd
lora_busy && echo "  meshtasticd left stopped: a co-lora-run tool has the radio" || sudo systemctl start meshtasticd
pipx install meshtastic >/dev/null 2>&1 || pipx upgrade meshtastic >/dev/null 2>&1 || true
M="$HOME/.local/bin/meshtastic"
if lora_busy; then echo "  region check skipped (meshtasticd paused; its saved region stays as set)"; else
for _ in $(seq 20); do "$M" --host localhost --info >/dev/null 2>&1 && break; sleep 3; done
# region is required before Meshtastic will transmit; US = 902-928 MHz ISM. Set once, never overwritten:
# outside the US run step 80 with CO_LORA_REGION=EU_868 (etc.) the first time.
"$M" --host localhost --get lora.region 2>/dev/null | grep -E 'region: ' | grep -qvE 'region: (0|UNSET)$' ||"$M" --host localhost --set lora.region "${CO_LORA_REGION:-US}"
"$M" --host localhost --get lora.region 2>/dev/null | tail -1
fi
