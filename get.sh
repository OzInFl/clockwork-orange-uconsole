#!/bin/bash
# Clockwork Orange one-line installer. Run it on the uConsole as your normal user (it uses sudo):
#   curl -fsSL https://raw.githubusercontent.com/OzInFl/clockwork-orange-uconsole/main/get.sh | bash
#   curl -fsSL .../get.sh | bash -s -- 60-tools 90-rfapps        # only some steps
#   curl -fsSL .../get.sh | CO_TZ=America/Chicago bash             # options go in front of bash
# Installs the latest published release into ~/clockwork-orange and runs install.sh. Safe to run again;
# after the first install, use Update Suite (co-update) instead.
set -euo pipefail
SLUG=${CO_REPO_SLUG:-OzInFl/clockwork-orange-uconsole}
DIR=${CO_HOME:-$HOME/clockwork-orange}
[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user, not root (steps use sudo where needed)."; exit 1; }
[ "$(uname -m)" = aarch64 ] || echo "!! not an arm64 system: Clockwork Orange targets the uConsole CM4 + AIO V2"
echo "[clockwork-orange] sudo is needed for packages and services:"; sudo -v
command -v git >/dev/null || { sudo apt-get update -qq; sudo apt-get install -y -qq git; }
tag=${CO_REF:-$(curl -fsSL "https://api.github.com/repos/$SLUG/releases/latest" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')}
if [ ! -d "$DIR/.git" ]; then
  [ -e "$DIR" ] && mv "$DIR" "$DIR.old-$(date +%s)"
  git clone -q "https://github.com/$SLUG" "$DIR"
fi
git -C "$DIR" fetch -q --tags --force origin
git -C "$DIR" -c advice.detachedHead=false checkout -q --force "$tag"
echo "[clockwork-orange] installing $tag"
# keep sudo alive through the long builds (OP25 takes about an hour on a CM4)
while true; do sudo -n true; sleep 50; kill -0 $$ 2>/dev/null || exit; done 2>/dev/null &
exec "$DIR/install.sh" "$@" < /dev/null
