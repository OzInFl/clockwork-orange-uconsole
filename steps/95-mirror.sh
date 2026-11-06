# Remote console: the uConsole screen + app launcher in any browser at https://<ip>:6080.
# co-mirror (mirror.py, HTTPS) serves the launcher + noVNC and relays /websockify to co-mirror-ws (websockify,
# 127.0.0.1:6081) -> co-mirror-vnc, a second wayvnc on 127.0.0.1:5901 with a login noVNC can speak (Apple-DH,
# checked by PAM against the uConsole account). Pi OS's own wayvnc on :5900 is left alone for regular VNC
# viewers: noVNC can't use any of its methods (VeNCrypt/TLS, RSA-AES 5/129). The password crosses the LAN only
# inside co-mirror's HTTPS (per-device self-signed cert, made here, never in the repo).
# Launches are limited to app ids in hub/apps.json (same as a Hub tile); rails/services stay Hub-only.
for p in novnc websockify wayvnc; do dpkg -s $p >/dev/null 2>&1 || sudo apt-get install -y -q $p; done
C=/etc/clockwork-orange/novnc.pem
if [ ! -f $C ]; then
  sudo install -d -m 0755 /etc/clockwork-orange
  sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$(hostname)" \
    -addext "subjectAltName=DNS:$(hostname),DNS:$(hostname).lan,DNS:localhost" -keyout $C -out $C 2>/dev/null
  sudo chown root:$(id -gn) $C; sudo chmod 0640 $C; echo "  made $C"
fi
# Web root = noVNC (symlinked, so package upgrades flow through) + the launcher page and server
D=/opt/clockwork-orange/mirror
changed=0
for f in index.html mirror.py; do put $D/$f && changed=1 || true; done
[ -d "$D/core" ] && [ ! -L "$D/core" ] && sudo rm -rf "$D/core"     # undo a patched copy from v1.0.2-dev
for x in /usr/share/novnc/*; do [ -e "$D/${x##*/}" ] || sudo ln -s "$x" "$D/"; done
put /etc/clockwork-orange/wayvnc-mirror.conf && changed=1 || true
for u in co-mirror-vnc co-mirror-ws co-mirror; do put /etc/systemd/system/$u.service && changed=1 || true; done
sudo systemctl daemon-reload
for u in co-mirror-vnc co-mirror-ws co-mirror; do
  sudo systemctl enable -q $u
  if [ $changed = 1 ] || ! systemctl is-active -q $u; then sudo systemctl restart $u; fi
done
sleep 3
curl -fsk -o /dev/null https://localhost:6080/vnc.html && echo "  remote console up: https://$(hostname -I | cut -d' ' -f1):6080" || echo "!! :6080 not answering"
systemctl is-active -q co-mirror-vnc || echo "!! co-mirror-vnc not running (needs the desktop session): journalctl -u co-mirror-vnc"
