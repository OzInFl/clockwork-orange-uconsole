# Remote console: the uConsole screen + app launcher in any browser at https://<ip>:6080.
# co-mirror (mirror.py, HTTPS) serves the launcher + noVNC and relays /websockify to co-mirror-ws (websockify,
# 127.0.0.1:6081) -> Pi OS's wayvnc :5900 (PAM login = the pi account password, RSA-AES encrypted). noVNC needs a
# secure context for RSA-AES, hence HTTPS with a per-device self-signed cert (made here, never in the repo).
# Launches are limited to app ids in hub/apps.json (same as a Hub tile); rails/services stay Hub-only.
for p in novnc websockify; do dpkg -s $p >/dev/null 2>&1 || sudo apt-get install -y -q $p; done
sudo systemctl enable -q --now wayvnc.service
C=/etc/clockwork-orange/novnc.pem
if [ ! -f $C ]; then
  sudo install -d -m 0755 /etc/clockwork-orange
  sudo openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$(hostname)" \
    -addext "subjectAltName=DNS:$(hostname),DNS:$(hostname).lan,DNS:localhost" -keyout $C -out $C 2>/dev/null
  sudo chown root:pi $C; sudo chmod 0640 $C; echo "  made $C"
fi
# Web root = noVNC (symlinked, so package upgrades flow through) + the launcher page and server
D=/opt/clockwork-orange/mirror
changed=0
for f in index.html mirror.py; do put $D/$f && changed=1 || true; done
for x in /usr/share/novnc/*; do [ -e "$D/${x##*/}" ] || sudo ln -s "$x" "$D/"; done
for u in co-mirror-ws co-mirror; do put /etc/systemd/system/$u.service && changed=1 || true; done
sudo systemctl daemon-reload
for u in co-mirror-ws co-mirror; do
  sudo systemctl enable -q $u
  if [ $changed = 1 ] || ! systemctl is-active -q $u; then sudo systemctl restart $u; fi
done
sleep 2
curl -fsk -o /dev/null https://localhost:6080/vnc.html && echo "  remote console up: https://$(hostname -I | cut -d' ' -f1):6080" || echo "!! :6080 not answering"
