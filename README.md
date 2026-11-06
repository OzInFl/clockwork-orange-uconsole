# Clockwork Orange

A receive-only RF field toolkit for the **ClockworkPi uConsole** (CM4) with the **HackerGadgets AIO V2**
board (RTL-SDR, LoRa, GPS). One installer turns a stock uConsole into a handheld scanner, decoder and
signal hunter: a P25 trunking analyzer, pager recorder, APRS map, aircraft / ship / balloon decoders,
weather-satellite recorder, Meshtastic + MeshCore, and a launcher hub, all usable on the 1280×720 screen
or from a browser on your LAN.

## Install

On the uConsole (terminal or SSH), as your normal user:

```bash
curl -fsSL https://raw.githubusercontent.com/OzInFl/clockwork-orange-uconsole/main/get.sh | bash
```

It installs the latest [release](https://github.com/OzInFl/clockwork-orange-uconsole/releases) into `~/clockwork-orange` and runs every step. The first run takes **about 1.5–2 hours**
(OP25 and several decoders are compiled on the CM4). Use a charger and keep it on Wi-Fi. Re-running is
safe: finished steps are skipped and only changed files are reinstalled. To update later, use **Update Suite**.

Options go in front of `bash`, and step names after `bash -s --`:

```bash
curl -fsSL .../get.sh | CO_TZ=America/Chicago bash          # also set the timezone
curl -fsSL .../get.sh | bash -s -- 60-tools 90-rfapps       # only these steps
~/clockwork-orange/install.sh 90-rfapps                     # after the first install
```

| Variable | Effect |
|---|---|
| `CO_TZ` | Set the timezone (e.g. `America/New_York`). Unset = leave it alone |
| `CO_LORA_REGION` | Meshtastic region on first setup (default `US`; e.g. `EU_868`) |
| `CO_FLASH_KB=1` | Flash the QMK keyboard firmware (trackball fix) in step 30. Off by default |
| `CO_LG_REPO` | Where step 50 clones Looking Glass from |

**Requirements:** uConsole with a CM4 and the AIO V2 board, Debian 13 (trixie) arm64 image with the labwc
desktop. Other boards and images are untested: step 20 drives the AIO V2's power GPIOs
(SDR 7, LoRa 16, GPS 27).

The Hub opens from the **Clockwork Orange** menu folder or desktop icon, or with `co-hub`.

## What you get

| App | Port | What it does |
|---|---|---|
| **Hub** | 8090 | Launcher + status (radio rails, services, temperature) for everything below |
| **[SDR Looking Glass](https://github.com/OzInFl/sdr-looking-glass-uconsole)** | 8080 | P25 trunking analyzer on OP25: live calls + audio, control-channel viewer, System Hunter, drive survey, diagnostics |
| **Pager Recorder** | 8091 | POCSAG / FLEX recorder with per-capcode labels, alert sounds, tone-only filter |
| **System Spy** | 8092 | Find, identify and decode signals: sweep, ACARS, VDL2, AIS, utility meters, digital voice IDs, Meteor-M weather satellites, Zigbee/Thread (with an nRF52840 sniffer) |
| **GPS Status** | 8093 | Sky plot, SNR, DOP, fix quality, grid square, antenna state |
| **APRS Monitor** | 8094 | APRS map (online tiles or offline radar view), stations, weather, messages, packet log |
| **Radiosondes** | 5000 | Weather-balloon tracking (radiosonde_auto_rx). SondeHub upload is off until you turn it on |
| **Remote console** | 6080 (https) | The uConsole's screen + app launcher in any browser |
| Meshtastic / MeshCore | 9443 | Both on the AIO V2's SX1262; one radio, so MeshCore pauses meshtasticd while it runs |
| Plus | | SDR++, GQRX, CubicSDR, Inspectrum, rtl_433, readsb (ADS-B), dsd-fme, direwolf, multimon-ng |

All the SDR apps share the one RTL: an app borrows it from Looking Glass and gives it back when it stops.
A second USB RTL-SDR is used first if plugged in.

**Receive only.** Nothing here transmits on the SDR. Meshtastic / MeshCore transmit on LoRa ISM, under
your region's rules. Monitoring laws differ by country and state; what you decode is your responsibility.

## Steps

| Step | What it does |
|---|---|
| `10-base` | Fixes the initramfs bug that breaks apt (`MODULES=most`), finishes interrupted upgrades keeping local configs, installs `packages.txt`, enables SSH, persistent journal |
| `20-sdr` | `co-rail` + `aiov2-rail@{sdr,gps}` boot services (AIO V2 power GPIOs), blacklists the DVB-T driver, checks `rtl_test` |
| `30-keyboard` | Stages j1n6 QMK (trackball fix; flashes only with `CO_FLASH_KB=1` and only over stock firmware). X/Y/A/B game buttons → F13–F16 + labwc bindings |
| `40-op25` | GNU Radio 3.10 + gr-osmosdr, builds boatbod OP25 at `~/op25/op25` (about 1 h on CM4) and patches in an RSSI probe. Skipped if already built at the same commit |
| `50-lookingglass` | Clones [SDR Looking Glass](https://github.com/OzInFl/sdr-looking-glass-uconsole) to `~/sdr-looking-glass` and runs it as a service on :8080 (`SDR_KIND=rtl`, pinned to the AIO RTL by serial). Per-unit ppm goes in `/etc/default/sdr-looking-glass` |
| `60-tools` | SDR++, GQRX, CubicSDR, Inspectrum, rtl_433, multimon-ng, direwolf, gpsd on the AIO GPS (`/dev/ttyS0`, read-only `-b` + NMEA init for the CASIC module), SoapySDR, field utils; builds readsb, AIS-catcher, libacars, acarsdec, dumpvdl2 |
| `70-desktop` | **Hub** (`co-hub`), "Clockwork Orange" menu folder + desktop icons, all generated from `hub/apps.json` |
| `80-lora` | SX1262 on SPI1: meshtasticd (web UI https://localhost:9443) + MeshCore (`co-lora-run`) |
| `90-rfapps` | APRS Monitor, Pager Recorder, System Spy, GPS Status (services `co-aprs`, `co-pager`, `co-spy`, `co-gps`; shared libs in `/opt/clockwork-orange/lib`) |
| `92-sonde-dsd` | radiosonde_auto_rx (`co-sonde`) and dsd-fme + mbelib (`co-dsd`) |
| `95-mirror` | Remote console: noVNC over HTTPS (per-device self-signed cert) → wayvnc :5900; log in with your uConsole account password |

`files/` mirrors absolute paths on the device (`files/etc/...` → `/etc/...`). Use `put /path` in a step to
install one. systemd units are written for user `pi`; `put` fills in the installing user's name, home and uid.

## Updates and releases

**Update Suite** (Hub tile, or `co-update` in a terminal) installs the latest *published* release from
[Releases](https://github.com/OzInFl/clockwork-orange-uconsole/releases), shows what's new, and re-runs the
installer, so new apps arrive with it. The Hub shows the installed version and an **Update to vX.Y.Z** button
when a newer release is out.

- `co-update v1.0.0`: install a specific release (also how you roll back)
- `CO_CHANNEL=beta co-update`: include pre-releases
- `co-update main`: the development branch, for testing

Versions are [semver](https://semver.org): `VERSION` is the suite's, `LOOKINGGLASS_VERSION` pins the Looking
Glass release it installs, and [CHANGELOG.md](CHANGELOG.md) lists every release. The Hub shows `+dev` after
the version when the install didn't come from a release tag.

**Publishing a release** (maintainers): work on `main`, then from a PC with `gh` logged in:

```powershell
.	oolselease.ps1 patch                        # or minor / major / 1.2.0;  -Notes "..." adds a summary
.	oolselease.ps1 1.2.0-beta.1 -Pre            # pre-release: only CO_CHANNEL=beta picks it up
.	oolselease.ps1 minor -DryRun                # preview the version and changelog
```

It bumps `VERSION`, writes the changelog section from the commits since the last tag, tags `vX.Y.Z`, pushes,
and publishes the GitHub Release. Users get it on their next Update Suite. To ship a new Looking Glass, release
it first (same script, `-Repo <its checkout>`), then put its version in `LOOKINGGLASS_VERSION` and release
the suite.

## Calibration

Each RTL dongle is a little off frequency. Find its ppm with Looking Glass's auto-calibrate on a known
P25 control channel (`rtl_test -p` is a rough first guess) and put it in `/etc/default/sdr-looking-glass`
as `SDR_PPM=<n>`. The other apps use the same value for the AIO RTL unless you set one in their settings.

## Developing from a Windows PC

```powershell
[Environment]::SetEnvironmentVariable("CO_TARGET", "pi@<uconsole-ip>", "User")   # once
.\deploy.ps1                                   # copy this folder to the uConsole and run everything
.\deploy.ps1 -Steps 20-sdr,90-rfapps           # some steps
.\deploy.ps1 -Background -Steps 40-op25        # long build, log: ~/clockwork-orange.log
.\sync-lookingglass.ps1                        # push a local Looking Glass checkout (CO_LG_SRC)
```

Build native code on the device, not on the PC: the uConsole is arm64.

## Adding a tool to the suite
Add one entry to `files/opt/clockwork-orange/hub/apps.json` (`id, name, desc, group, icon, exec`, plus
`terminal: true` for CLI tools, `sdr: true` if it needs the RTL, `desktop: true` for a desktop icon, `web`
for a URL the remote console opens in a PC tab). Put its install in a step and an icon in
`files/opt/clockwork-orange/icons/` (64x64 SVG, `co-*`). Then run step `70-desktop`. The hub tile, menu
entry and desktop icon all launch through `co-launch <id>`. `sdr: true` wraps the tool in `co-sdr-run`,
which pauses Looking Glass (it holds the RTL) and resumes it on exit.

## Device commands
`co-hub` (hub window) · `co-launch <id>` · `co-rail <sdr|gps|lora|usb|all> [on|off|status]` ·
`co-sdr-run <cmd>` · `co-lora-run <cmd>` · `co-aprs` · `co-pager` · `co-spy` · `co-gps` · `co-sonde` ·
`co-dsd [MHz] [mode]` · `co-lookingglass` · `co-ccviewer`

## Credits

Built on [OP25](https://github.com/boatbod/op25) (boatbod), GNU Radio, rtl-sdr, multimon-ng, rtl_433,
[readsb](https://github.com/wiedehopf/readsb), [AIS-catcher](https://github.com/jvde-github/AIS-catcher),
[acarsdec](https://github.com/TLeconte/acarsdec), [libacars + dumpvdl2](https://github.com/szpajder),
[SatDump](https://github.com/SatDump/SatDump), [radiosonde_auto_rx](https://github.com/projecthorus/radiosonde_auto_rx),
[dsd-fme](https://github.com/lwvmobile/dsd-fme), direwolf, aprslib, Leaflet, noVNC, wayvnc, Meshtastic,
[meshcore-uconsole](https://github.com/cwill747), and [j1n6's QMK for the uConsole](https://github.com/j1n6/qmk-uconsole).
Each is under its own license; none are bundled here, the steps fetch them.

## License

MIT (this repo's scripts and apps). See [LICENSE](LICENSE).
