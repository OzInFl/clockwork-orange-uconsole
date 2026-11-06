# Desktop integration: the Clockwork Orange Hub app, a "Clockwork Orange" start-menu folder generated
# from hub/apps.json, and desktop icons. apps.json is the single list of suite apps.
for f in co-webapp co-launch co-hub co-lookingglass co-ccviewer co-meshtastic-web; do put /usr/local/bin/$f 0755 || true; done
for f in hub.py index.html apps.json; do put /opt/clockwork-orange/hub/$f || true; done
for f in "$CO_DIR"/files/opt/clockwork-orange/icons/*.svg; do
  n=$(basename "$f"); put /opt/clockwork-orange/icons/$n || true
  sudo install -D -m 0644 "$f" /usr/local/share/icons/hicolor/scalable/apps/$n
done
sudo gtk-update-icon-cache -q /usr/local/share/icons/hicolor 2>/dev/null || true
put /etc/xdg/menus/applications-merged/clockwork-orange.menu || true
put /usr/local/share/desktop-directories/clockwork-orange.directory || true

# .desktop files: the hub itself + one per app (menu only lists Category X-ClockworkOrange)
A=/usr/local/share/applications; sudo mkdir -p "$A"; sudo rm -f "$A"/co-*.desktop
desk() {  # id name comment icon exec
  printf '[Desktop Entry]\nType=Application\nName=%s\nComment=%s\nIcon=%s\nExec=%s\nTerminal=false\nCategories=X-ClockworkOrange;\nStartupWMClass=%s\n' \
    "$2" "$3" "$4" "$5" "$1" | sudo tee "$A/co-$1.desktop" >/dev/null
}
desk hub "Clockwork Orange" "uConsole RF suite launcher" co-hub co-hub
python3 - "$A" <<'PY' | while IFS=$'\t' read -r id name desc icon d; do desk "$id" "$name" "$desc" "$icon" "co-launch $id"; done
import json; [print("\t".join([a["id"], a["name"], a["desc"], a["icon"], "1" if a.get("desktop") else "0"]))
 for a in json.load(open("/opt/clockwork-orange/hub/apps.json"))["apps"]]
PY
sudo update-desktop-database -q "$A" 2>/dev/null || true

# desktop icons (hub + apps flagged "desktop"); quick_exec stops pcmanfm's "execute this file?" prompt
mkdir -p "$HOME/Desktop"; rm -f "$HOME/Desktop"/co-*.desktop
cp "$A/co-hub.desktop" "$HOME/Desktop/"
python3 -c 'import json;[print(a["id"]) for a in json.load(open("/opt/clockwork-orange/hub/apps.json"))["apps"] if a.get("desktop")]' \
  | while read -r id; do cp "$A/co-$id.desktop" "$HOME/Desktop/"; done
chmod +x "$HOME/Desktop"/co-*.desktop
C="$HOME/.config/libfm/libfm.conf"; mkdir -p "$(dirname "$C")"; [ -f "$C" ] || printf '[config]\n' > "$C"
grep -q '^quick_exec=' "$C" && sed -i 's/^quick_exec=.*/quick_exec=1/' "$C" || sed -i '/^\[config\]/a quick_exec=1' "$C"
echo "  menu: $(ls $A | grep -c '^co-') entries; desktop: $(ls $HOME/Desktop | grep '^co-' | tr '\n' ' ')"
# restart a running hub server so code changes apply. It must carry the full desktop-session env
# (DISPLAY, WAYLAND_DISPLAY, DBUS...) or apps it launches can't open windows - take it from labwc.
if pgrep -f "[h]ub.py serve" >/dev/null; then
  pkill -f "[h]ub.py serve"; sleep 0.5
  lp=$(pgrep -u "$USER" -x labwc | head -1)
  if [ -n "$lp" ]; then
    ( while IFS= read -r -d '' kv; do export "$kv"; done < /proc/$lp/environ
      setsid nohup python3 /opt/clockwork-orange/hub/hub.py serve >/tmp/co-hub-server.log 2>&1 < /dev/null & )
    echo "  hub server restarted in the desktop session"
  fi
fi
