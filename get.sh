#!/bin/bash
# Clockwork Orange one-line installer. Run it on the uConsole as your normal user (it uses sudo):
#   curl -fsSL https://raw.githubusercontent.com/OzInFl/clockwork-orange-uconsole/main/get.sh | bash
#   curl -fsSL .../get.sh | bash -s -- 60-tools 90-rfapps        # only some steps
#   curl -fsSL .../get.sh | CO_TZ=America/Chicago bash             # options go in front of bash
# Clones (or updates) the repo in ~/clockwork-orange, then runs install.sh. Safe to run again.
set -euo pipefail
REPO=${CO_REPO:-https://github.com/OzInFl/clockwork-orange-uconsole}
DIR=${CO_HOME:-$HOME/clockwork-orange}
[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user, not root (steps use sudo where needed)."; exit 1; }
[ "$(uname -m)" = aarch64 ] || echo "!! not an arm64 system: Clockwork Orange targets the uConsole CM4 + AIO V2"
echo "[clockwork-orange] sudo is needed for packages and services:"; sudo -v
command -v git >/dev/null || { sudo apt-get update -qq; sudo apt-get install -y -qq git; }
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q --ff-only
else
  [ -e "$DIR" ] && mv "$DIR" "$DIR.old-$(date +%s)"
  git clone -q "$REPO" "$DIR"
fi
# keep sudo alive through the long builds (OP25 takes about an hour on a CM4)
while true; do sudo -n true; sleep 50; kill -0 $$ 2>/dev/null || exit; done 2>/dev/null &
exec "$DIR/install.sh" "$@" < /dev/null
