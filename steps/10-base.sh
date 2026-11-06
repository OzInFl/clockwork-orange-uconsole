# Base OS: unblock apt on the uConsole image, finish interrupted upgrades, install base packages.
put /etc/initramfs-tools/conf.d/uconsole-modules.conf || true
# Keep local conffiles (e.g. /etc/xdg/labwc/environment has uConsole display/cursor tweaks).
sudo dpkg --force-confold --configure -a
sudo apt-get update -qq
sudo apt-get -y -o Dpkg::Options::=--force-confold upgrade
sudo apt-get install -y -qq $(grep -vE '^\s*(#|$)' "$CO_DIR/packages.txt")
sudo systemctl enable --now ssh
# Field logs need local time. Set it with CO_TZ=America/Chicago (etc.); otherwise the timezone is left alone,
# with a hint when it's still the image's America/Adak default.
if [ -n "${CO_TZ:-}" ]; then sudo timedatectl set-timezone "$CO_TZ"
elif [ "$(timedatectl show -p Timezone --value)" = America/Adak ]; then echo "  timezone is America/Adak (image default): re-run with CO_TZ=<your zone> or use: sudo timedatectl set-timezone <zone>"; fi

# Persistent journal: field sessions (often several boots, no network) must survive a reboot for review.
if [ ! -d /var/log/journal ]; then
  sudo mkdir -p /var/log/journal && sudo systemd-tmpfiles --create --prefix /var/log/journal
  sudo systemctl restart systemd-journald; echo "  journal: now persistent (/var/log/journal)"
fi
