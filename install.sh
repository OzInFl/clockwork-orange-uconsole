#!/bin/bash
# Clockwork Orange - provision a ClockworkPi uConsole (CM4 + HackerGadgets AIO V2) for our RF toolkit.
# Idempotent: safe to re-run. Usage: ./install.sh            (all steps)
#                                    ./install.sh 20-sdr 40-op25   (named steps)
set -euo pipefail
CO_DIR="$(cd "$(dirname "$0")" && pwd)"; export CO_DIR
export DEBIAN_FRONTEND=noninteractive
log() { printf '\n\033[1;33m[clockwork-orange] %s\033[0m\n' "$*"; }
# install a file from files/ to its absolute path; returns 0 if it changed.
# systemd units are written for user pi (uid 1000); they're rendered for whoever runs the installer.
put() {
  local src="$CO_DIR/files$1"
  if [[ $1 == /etc/systemd/* ]]; then
    local r; r=$(mktemp); sed -e "s#/home/pi\b#$HOME#g" -e "s#^User=pi\$#User=$(id -un)#" -e "s#/run/user/1000\b#/run/user/$(id -u)#g" "$src" > "$r"; src=$r
  fi
  sudo cmp -s "$src" "$1" 2>/dev/null && return 1; sudo install -D -m "${2:-0644}" "$src" "$1"; echo "  installed $1"
}
export -f log put
steps=("$@"); [ ${#steps[@]} -eq 0 ] && steps=($(cd "$CO_DIR/steps" && ls *.sh | sed 's/\.sh$//'))
for s in "${steps[@]}"; do log "step $s"; bash -e "$CO_DIR/steps/$s.sh"; done
log "done: ${steps[*]}"
