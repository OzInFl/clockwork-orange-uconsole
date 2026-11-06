# Changelog

Clockwork Orange versions follow [semver](https://semver.org). Each release is a git tag `vX.Y.Z` with a GitHub
Release; Update Suite (`co-update`) and `get.sh` install the latest published one. `LOOKINGGLASS_VERSION` pins
the [SDR Looking Glass](https://github.com/OzInFl/sdr-looking-glass-uconsole) release each suite version installs.

## [1.1.0] - 2026-11-06
New apps: ALPR Watch (Flock / Raven detector + known ALPR camera alerts) and Wireless Census (Bluetooth + Wi-Fi device census, tracker-following alarm), in the Hub's new Surveillance group.

- Add ALPR Watch and Wireless Census apps

## [1.0.3] - 2026-11-06
The remote console's live screen now works in the browser (log in with your uConsole account).

- Remote console: the live screen now connects in the browser

## [1.0.2] - 2026-11-06
- Looking Glass 1.0.1: no false 'OP25 software error' when a browser audio tab closes

## [1.0.1] - 2026-11-06
Fixes from the first field update test.

- install.sh: mark a checkout with local changes as +dev
- LF everywhere (a .ps1 eol=crlf rule made fresh Linux clones look modified)

## [1.0.0] - 2026-11-06
First public release. Looking Glass 1.0.0.
- One-line installer (`get.sh`) and Update Suite (`co-update`) that install published releases
- Hub with versions and an update-available button; new apps appear without reopening it
- SDR Looking Glass (P25), Pager Recorder, System Spy (ACARS, VDL2, AIS, meters, dsd-fme, Meteor-M weather sats),
  GPS Status, APRS Monitor, radiosondes, dsd-fme Digital Voice, Meshtastic + MeshCore, remote console
- Installs for whichever user runs it (systemd units are rendered per user); timezone and LoRa region are options
