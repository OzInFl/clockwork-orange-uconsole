#!/usr/bin/env python3
"""op25_rssi_patch.py <op25 apps dir> - add a channel power (RSSI) reading to boatbod OP25. Idempotent.

p25_demodulator[_dev].py: a probe_avg_mag_sqrd_c on the channel-filtered IF (if_out, ~0.1 s averaging) and
                    get_level_db() -> dBFS (relative: fixed RTL gain makes it comparable for antenna work).
multi_rx.py:        ui_freq_update() reports it per channel as 'level' next to 'error'/'ppm', which is
                    what SDR Looking Glass already polls from the OP25 terminal.
tk_p25.py:          [lg-sndcp] the 0x16 SNDCP data channel announcement also prints aa/ra/dso/rsv/dac
                    (OP25 only printed the two channels); appended after ch2 so older parsers still match.
                    [lg-sndcp2] only for MFID 0: Motorola's 0x16 (TDMA data channel) goes to the 'unhandled' print.
Python-only, so no rebuild is needed.
"""
import os, sys

apps = sys.argv[1] if len(sys.argv) > 1 else "."

def patch(name, pairs, marker, required=True):
    p = os.path.join(apps, name)
    if not os.path.exists(p) and not required: return
    s = open(p).read()
    if marker in s:
        print("  %s: already patched" % name); return
    for a, b in pairs:
        if s.count(a) != 1:
            if not required: print("  %s: skipped (layout differs)" % name); return
            sys.exit("!! %s: anchor not found (OP25 changed?): %r" % (name, a[:60]))
        s = s.replace(a, b)
    open(p, "w").write(s); print("  %s: patched" % name)

DEMOD = [
    ("from math import pi, isnan, isinf\n", "from math import pi, isnan, isinf, log10\n"),
    ("        self.agc = rms_agc.rms_agc(0.45, 0.85)\n",
     "        self.agc = rms_agc.rms_agc(0.45, 0.85)\n"
     "        # [lg-rssi] channel power probe: mean |x|^2 of the channel-filtered IF, ~0.1 s averaging\n"
     "        self.level_probe = analog.probe_avg_mag_sqrd_c(-200, 1.0 / (self.if_rate * 0.1))\n"
     "        self.connect(self.if_out, self.level_probe)\n"),
    ("    def get_freq_error(self):   # get frequency error from FLL and convert to Hz\n",
     "    def get_level_db(self):     # [lg-rssi] channel power in dBFS (relative)\n"
     "        v = self.level_probe.level()\n"
     "        return round(10 * log10(v), 1) if v > 0 else None\n\n"
     "    def get_freq_error(self):   # get frequency error from FLL and convert to Hz\n"),
]
# multi_rx imports p25_demodulator_dev (as p25_demodulator); rx.py uses p25_demodulator - patch both
patch("p25_demodulator_dev.py", DEMOD, "[lg-rssi]")
patch("p25_demodulator.py", DEMOD, "[lg-rssi]", required=False)

patch("multi_rx.py", [
    ("    def get_error(self):\n        return self.error\n",
     "    def get_error(self):\n        return self.error\n\n"
     "    def get_level(self):        # [lg-rssi]\n"
     "        f = getattr(self.demod, 'get_level_db', None)\n"
     "        return f() if f else None\n"),
    ("            params[rx_id]['error'] = self.find_channel(int(rx_id)).get_error()\n",
     "            params[rx_id]['error'] = self.find_channel(int(rx_id)).get_error()\n"
     "            params[rx_id]['level'] = self.find_channel(int(rx_id)).get_level()   # [lg-rssi]\n"),
], "[lg-rssi]")

patch("tk_p25.py", [
    ("                sys.stderr.write('%s [%d] tsbk(0x16) sndcp_data_ch: ch1: %x ch2: %x\\n' % (log_ts.get(), m_rxid, ch1, ch2))\n",
     "                sys.stderr.write('%s [%d] tsbk(0x16) sndcp_data_ch: ch1: %x ch2: %x aa: %d ra: %d dso: 0x%02x rsv: 0x%02x dac: 0x%04x\\n' % (log_ts.get(), m_rxid, ch1, ch2,   # [lg-sndcp]\n"
     "                    (tsbk >> 71) & 1, (tsbk >> 70) & 1, (tsbk >> 72) & 0xff, (tsbk >> 64) & 0x3f, (tsbk >> 16) & 0xffff))\n"),
], "[lg-sndcp]")
# [lg-sndcp2] tk_p25 decodes every opcode 0x16 as SNDCP, but MFID 0x90 0x16 is Motorola's TDMA data channel
# announcement (different layout): let it fall through to the 'unhandled' hex print, which Looking Glass decodes
patch("tk_p25.py", [
    ("        elif opcode == 0x16:   # sndcp data ch\n",
     "        elif opcode == 0x16 and ((tsbk >> 80) & 0xff) == 0:   # sndcp data ch [lg-sndcp2]\n"),
], "[lg-sndcp2]")
