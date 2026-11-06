#!/usr/bin/env python3
"""cps2lg - convert a Motorola CPS trunking export (import_export_doc XML) into SDR Looking Glass imports.

  python cps2lg.py "WPB Harris Trunking.xml" "000A WPB" out_dir [--name "..."] [--county "..."]

Writes out_dir/systems.json (POST to /api/systems/import) and out_dir/talkgroups.csv
(id,name for /api/import, type=tg). Only talkgroups whose TG System ID matches the chosen
system are taken; secure-strapped talkgroups get a " [SEC]" suffix.
CPS stores WACN / System ID in decimal; Looking Glass uses hex (WACN "0xBEE00", SysID "544").
"""
import csv, json, os, re, sys, xml.etree.ElementTree as ET

def fields(sec):
    return {f.get("Name"): (f.text or "").strip() for f in sec.iter("Field")}

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = dict(zip(sys.argv[1::1], sys.argv[2::1]))
    src, sysname, out = args[0], args[1], args[2]
    root = ET.parse(src).getroot()
    node = next(n for n in root.iter("Node") if n.get("Name") == "Trunking System" and n.get("ReferenceKey") == sysname)
    gen = fields(next(s for s in node.iter("Section") if s.get("Name") == "General"))
    dig = fields(next(s for s in node.iter("Section") if s.get("Name") == "Digital"))
    sysid_dec, wacn_dec = int(gen["System ID"]), int(gen["Home WACN ID"])
    ccs = []
    for es in node.iter("EmbeddedSection"):
        if es.get("Name") == "Control Channels":
            f = "%.5f" % float(fields(es)["Rx Frequency (MHz)"])
            f = f.rstrip("0").rstrip(".") if "." in f else f
            if f not in ccs: ccs.append(f)
    sid = re.sub(r"\W+", "-", sysname.lower()).strip("-")
    rec = {"name": opts.get("--name") or "%s (CPS import)" % sysname, "county": opts.get("--county", ""),
           "type": "P25", "phase2": any(fields(s).get("Phase 2 Voice Capable") == "True" for s in node.iter("Section")),
           "nac": "", "wacn": "0x%05X" % wacn_dec, "sysid": "%03X" % sysid_dec,
           "control_channels": ccs, "modulation": "cqpsk" if dig.get("Digital Modulator Type", "").upper() == "CQPSK" else "fsk4"}
    tgs = {}
    for es in root.iter("EmbeddedSection"):
        if es.get("Name") != "Talkgroup": continue
        f = fields(es)
        if f.get("TG System ID") != str(sysid_dec): continue
        name = f["Talkgroup Name"].rstrip("*.") + (" [SEC]" if f.get("Secure / Clear Strapping") == "Secure" else "")
        tgs[int(f["Talkgroup ID"])] = name
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "systems.json"), "w") as fh: json.dump({"systems": {sid: rec}}, fh, indent=2)
    with open(os.path.join(out, "talkgroups.csv"), "w", newline="") as fh:
        w = csv.writer(fh); [w.writerow([k, v]) for k, v in sorted(tgs.items())]
    print(json.dumps(rec, indent=2)); print("%d talkgroups" % len(tgs))

if __name__ == "__main__":
    main()
