"""co_decode - parsers shared by the Clockwork Orange apps.

parse_multimon(line)  multimon-ng 1.3 text -> dict (POCSAG 512/1200/2400, FLEX old ': ' and new '|' formats, APRS (-A), EAS/SAME)
parse_154(hex)        IEEE 802.15.4 MAC frame (nRF 802.15.4 sniffer) -> dict, with Zigbee NWK / Thread (6LoWPAN, MLE beacons) hints
"""
import re, struct

# ------------------------------------------------------------------ paging
_POC = re.compile(r"^POCSAG(512|1200|2400):\s+Address:\s*(\d+)\s+Function:\s*(\d)\s*(?:(Alpha|Numeric|Skyper):\s?(.*))?$")
_FLEX_NEW = re.compile(r"^FLEX(?:_NEXT)?\|([^|]*)\|(\d+)/(\d)/(\w)(?:/(\w))?\|(\d+)\.(\d+)\|([\d ]+)\|(\w+)\|(.*)$")
_FLEX_OLD = re.compile(r"^FLEX(?:_NEXT)?:\s+(\S+ \S+)\s+(\d+)/(\d)/(\w)(?:/(\w))?\s+(\d+)\.(\d+)\s+\[([\d ]+)\]\s+(\w+)\s+(.*)$")
_CTRL = re.compile(r"<(?:NUL|ETX|EOT|ETB|DLE|DC\d|SOH|STX|ENQ|ACK|BEL|BS|LF|CR|VT|FF|SO|SI|NAK|SYN|CAN|EM|SUB|ESC|FS|GS|RS|US|DEL)>")
FLEX_TYPES = {"ALN": "alpha", "NUM": "numeric", "TON": "tone", "BIN": "binary", "SEC": "secure", "UNK": "unknown", "HEX": "hex"}

def _clean(m):
    m = _CTRL.sub(lambda x: "\n" if x.group(0) in ("<LF>", "<CR>") else "", m or "")
    return re.sub(r"\n{2,}", "\n", m).strip()

# ------------------------------------------------------------------ EAS / SAME (NWR)
SAME_ORG = {"EAS": "EAS participant (broadcaster)", "CIV": "Civil authorities", "WXR": "National Weather Service", "PEP": "Primary Entry Point (national)"}
SAME_EVT = {
    "EAN": "Emergency Action Notification", "EAT": "Emergency Action Termination", "NIC": "National Information Center", "NPT": "National Periodic Test",
    "RMT": "Required Monthly Test", "RWT": "Required Weekly Test", "ADR": "Administrative Message", "AVW": "Avalanche Warning", "AVA": "Avalanche Watch",
    "BZW": "Blizzard Warning", "CAE": "Child Abduction Emergency", "CDW": "Civil Danger Warning", "CEM": "Civil Emergency Message",
    "CFW": "Coastal Flood Warning", "CFA": "Coastal Flood Watch", "DSW": "Dust Storm Warning", "EQW": "Earthquake Warning", "EVI": "Evacuation Immediate",
    "EWW": "Extreme Wind Warning", "FRW": "Fire Warning", "FFW": "Flash Flood Warning", "FFA": "Flash Flood Watch", "FFS": "Flash Flood Statement",
    "FLW": "Flood Warning", "FLA": "Flood Watch", "FLS": "Flood Statement", "HMW": "Hazardous Materials Warning", "HWW": "High Wind Warning",
    "HWA": "High Wind Watch", "HUW": "Hurricane Warning", "HUA": "Hurricane Watch", "HLS": "Hurricane Statement", "LEW": "Law Enforcement Warning",
    "LAE": "Local Area Emergency", "NMN": "Network Message Notification", "TOE": "911 Telephone Outage Emergency", "NUW": "Nuclear Power Plant Warning",
    "DMO": "Practice/Demo Warning", "RHW": "Radiological Hazard Warning", "SVR": "Severe Thunderstorm Warning", "SVA": "Severe Thunderstorm Watch",
    "SVS": "Severe Weather Statement", "SPW": "Shelter in Place Warning", "SMW": "Special Marine Warning", "SPS": "Special Weather Statement",
    "SSA": "Storm Surge Watch", "SSW": "Storm Surge Warning", "TOR": "Tornado Warning", "TOA": "Tornado Watch", "TRW": "Tropical Storm Warning",
    "TRA": "Tropical Storm Watch", "TSW": "Tsunami Warning", "TSA": "Tsunami Watch", "VOW": "Volcano Warning", "WSW": "Winter Storm Warning", "WSA": "Winter Storm Watch"}
_EAS = re.compile(r"ZCZC-(\w{3})-(\w{3})-([\d-]+)\+(\d{4})-(\d{7})-([^-]+)-?")

def parse_same(s):
    m = _EAS.search(s)
    if not m: return None
    org, evt, fips, purge, issued, sender = m.groups()
    areas = [f for f in fips.split("-") if f]
    d = {"type": "EAS", "org": org, "org_txt": SAME_ORG.get(org, org), "event": evt, "event_txt": SAME_EVT.get(evt, "event " + evt),
         "fips": areas, "purge": ("%d h " % int(purge[:2]) if int(purge[:2]) else "") + "%d min" % int(purge[2:]), "issued": "day %s %s:%s UTC" % (issued[:3], issued[3:5], issued[5:]),
         "sender": sender.strip(), "raw": m.group(0)}
    d["text"] = "%s from %s for %d area(s) (%s), valid %s, issued %s by %s" % (d["event_txt"], d["org_txt"], len(areas),
               ", ".join("FIPS " + a for a in areas[:6]) + (" ..." if len(areas) > 6 else ""), d["purge"], d["issued"], d["sender"])
    et = d["event_txt"]
    d["severity"] = "crit" if "Warning" in et or "Emergency" in et or evt in ("EAN", "EVI") else "warn" if "Watch" in et or "Statement" in et else "info"
    return d

# ------------------------------------------------------------------ APRS (multimon -A, TNC2 text)
_APRS = re.compile(r"^APRS:\s*([A-Z0-9-]+)>([A-Z0-9-]+)((?:,[A-Za-z0-9*-]+)*):(.*)$")
_POS = re.compile(r"[!=/@](?:\d{6}[hz/])?(\d{2})(\d{2}\.\d+)([NS])(.)(\d{3})(\d{2}\.\d+)([EW])(.)(.*)")

def parse_aprs(line):
    m = _APRS.match(line.strip())
    if not m: return None
    src, dst, path, info = m.groups()
    d = {"type": "APRS", "src": src, "dst": dst, "path": path.lstrip(","), "info": info}
    p = _POS.search(info)
    if p:
        la = int(p.group(1)) + float(p.group(2)) / 60; lo = int(p.group(5)) + float(p.group(6)) / 60
        d.update(lat=round(-la if p.group(3) == "S" else la, 5), lon=round(-lo if p.group(7) == "W" else lo, 5),
                 symbol=p.group(4) + p.group(8), comment=p.group(9).strip())
    d["text"] = "%s → %s%s: %s" % (src, dst, (" via " + d["path"]) if d["path"] else "",
                                  ("position %.4f, %.4f %s" % (d["lat"], d["lon"], d.get("comment", ""))) if "lat" in d else info)
    return d

def parse_multimon(line):
    line = line.rstrip("\r\n")
    m = _POC.match(line)
    if m:
        baud, ric, fn, kind, msg = m.groups()
        return {"type": "POCSAG", "proto": "POCSAG" + baud, "baud": int(baud), "ric": int(ric), "func": int(fn),
                "kind": (kind or "tone").lower(), "msg": _clean(msg)}
    for rx, new in ((_FLEX_NEW, True), (_FLEX_OLD, False)):
        m = rx.match(line)
        if m:
            ts, baud, lvl, phase, frag, cyc, frame, caps, typ, msg = m.groups()
            caps = [int(c) for c in caps.split()]
            # multimon-ng prints a bit-errored long address as a negative number cast to unsigned (e.g. 4294934552):
            # no real capcode is >= 2^31 or 0, so those frames are corrupt
            bad = any(c <= 0 or c >= 2 ** 31 for c in caps)
            msg = _clean(msg)
            if not bad and len(msg) >= 16 and len(set(msg)) <= 2: bad = True          # "000000…" / "[[[[…" fill = bit errors
            return {"corrupt": bad, "type": "FLEX", "proto": "FLEX%s/%s" % (baud, lvl), "baud": int(baud), "ric": caps[0], "rics": caps, "func": None,
                    "kind": FLEX_TYPES.get(typ, typ.lower()), "msg": _clean(msg), "flex_cycle": int(cyc), "flex_frame": int(frame), "phase": phase}
    if line.startswith("APRS:"): return parse_aprs(line)
    if line.startswith("EAS:") and "ZCZC" in line: return parse_same(line)
    return None

# ------------------------------------------------------------------ IEEE 802.15.4 (nRF52840 sniffer)
FTYPE = {0: "beacon", 1: "data", 2: "ack", 3: "MAC command", 4: "reserved", 5: "multipurpose", 6: "fragment", 7: "extended"}
MACCMD = {1: "association request", 2: "association response", 3: "disassociation", 4: "data request", 5: "PAN ID conflict",
          6: "orphan notification", 7: "beacon request", 8: "coordinator realignment", 9: "GTS request"}

def _addr(b, i, mode):
    if mode == 2: return "0x%04x" % struct.unpack_from("<H", b, i)[0], i + 2
    if mode == 3: return ":".join("%02x" % x for x in b[i:i + 8][::-1]), i + 8
    return None, i

def parse_154(hexs, has_fcs=True):
    """Decode one sniffed frame. The nRF 802.15.4 sniffer passes the 2 FCS bytes through; they're dropped here."""
    try: b = bytes.fromhex(hexs)
    except ValueError: return None
    if has_fcs and len(b) > 3: b = b[:-2]
    if len(b) < 3: return None
    fc = struct.unpack_from("<H", b, 0)[0]
    ft, sec, comp = fc & 7, (fc >> 3) & 1, (fc >> 6) & 1
    dm, ver, sm = (fc >> 10) & 3, (fc >> 12) & 3, (fc >> 14) & 3
    d = {"ftype": FTYPE.get(ft, ft), "secure": bool(sec), "ver": ver, "seq": b[2], "len": len(b)}
    i = 3
    try:
        if dm: d["dst_pan"] = "0x%04x" % struct.unpack_from("<H", b, i)[0]; i += 2
        d["dst"], i = _addr(b, i, dm)
        if sm:
            if not comp: d["src_pan"] = "0x%04x" % struct.unpack_from("<H", b, i)[0]; i += 2
            else: d["src_pan"] = d.get("dst_pan")
        d["src"], i = _addr(b, i, sm)
    except struct.error:
        d["proto"] = "802.15.4 (truncated)"; return d
    p = b[i:]; d["pan"] = d.get("src_pan") or d.get("dst_pan")
    proto = "802.15.4"
    if ft == 2: proto = "802.15.4 ack"
    elif ft == 3 and not sec and p: d["cmd"] = MACCMD.get(p[0], "command 0x%02x" % p[0]); proto = "802.15.4 " + d["cmd"]
    elif ft == 0 and len(p) >= 4:                                  # beacon: superframe(2) GTS(1) pending(1) then the payload
        bp = p[4:]
        if bp[:1] == b"\x00" and len(bp) >= 15:
            proto = "Zigbee beacon"; d["stack"] = bp[1] & 0x0f; d["ext_pan"] = ":".join("%02x" % x for x in bp[3:11][::-1])
            d["permit_join"] = bool((struct.unpack_from("<H", p, 0)[0] >> 15) & 1)
        elif bp[:1] == b"\x03" and len(bp) >= 18:
            proto = "Thread beacon"; d["net_name"] = bp[2:18].rstrip(b"\x00").decode(errors="replace")
            if len(bp) >= 26: d["ext_pan"] = ":".join("%02x" % x for x in bp[18:26])
        else: proto = "802.15.4 beacon"
    elif ft == 1:
        if sec: proto = "Thread / 802.15.4 (MAC-encrypted)" if ver >= 1 else "802.15.4 (MAC-encrypted)"
        elif len(p) >= 8 and ((p[0] | p[1] << 8) >> 2) & 0xf == 2 and (p[0] & 3) in (0, 1):
            nfc = p[0] | p[1] << 8
            d["nwk_dst"] = "0x%04x" % struct.unpack_from("<H", p, 2)[0]; d["nwk_src"] = "0x%04x" % struct.unpack_from("<H", p, 4)[0]
            d["nwk_secure"] = bool((nfc >> 9) & 1)
            proto = "Zigbee NWK %s%s" % ("command" if (nfc & 3) == 1 else "data", " (encrypted)" if d["nwk_secure"] else "")
        elif p[:1] and (0x60 <= p[0] <= 0x7f or p[0] == 0x41 or 0xc0 <= p[0] <= 0xc7 or 0xe0 <= p[0] <= 0xe7 or 0x80 <= p[0] <= 0xbf):
            proto = "6LoWPAN (Thread / IPv6)"
        else: proto = "802.15.4 data"
    d["proto"] = proto
    d["family"] = "Zigbee" if proto.startswith("Zigbee") else "Thread" if ("Thread" in proto or "6LoWPAN" in proto) else "802.15.4"
    return d
