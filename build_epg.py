#!/usr/bin/env python3
"""Rebuild the M3UPT guide so its channel IDs match the IDs in the IPTV provider's playlist.

Reads mapping.json ({"map": {provider_id: m3upt_id}}), downloads the M3UPT XMLTV guide
(or reads a local file), keeps only the mapped channels, renames them to the provider's
IDs and writes a gzipped XMLTV file.

Usage:
    python3 build_epg.py                       # download from the URL in mapping.json
    python3 build_epg.py --input epg.xml.gz    # use a local copy (xml or xml.gz)
    python3 build_epg.py --output docs/epg.xml.gz
"""
import argparse
import gzip
import io
import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

MIN_CHANNELS = 50  # refuse to publish a guide that is clearly broken


def open_source(src):
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (epg-pt-remap)"})
        data = urllib.request.urlopen(req, timeout=120).read()
    else:
        data = Path(src).read_bytes()
    if data[:2] == b"\x1f\x8b":  # gzip magic number
        data = gzip.decompress(data)
    return io.BytesIO(data)


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="local XMLTV file (.xml or .xml.gz); default: download")
    ap.add_argument("--mapping", default=str(here / "mapping.json"))
    ap.add_argument("--output", default=str(here / "docs" / "epg.xml.gz"))
    args = ap.parse_args()

    cfg = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
    # m3upt id -> every provider id that should get a copy of it
    targets = defaultdict(list)
    for provider_id, m3upt_id in cfg["map"].items():
        targets[m3upt_id].append(provider_id)

    src = open_source(args.input or cfg["source_url"])

    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<tv generator-info-name="epg-pt-remap">\n')
    channels_written = set()
    programmes = 0

    for _event, el in ET.iterparse(src, events=("end",)):
        if el.tag == "channel":
            for pid in targets.get(el.get("id"), ()):
                el.set("id", pid)
                out.write(ET.tostring(el, encoding="unicode").strip() + "\n")
                channels_written.add(pid)
                el.set("id", "")  # avoid accidental reuse
            el.clear()
        elif el.tag == "programme":
            ids = targets.get(el.get("channel"), ())
            for pid in ids:
                el.set("channel", pid)
                out.write(ET.tostring(el, encoding="unicode").strip() + "\n")
                programmes += 1
            el.clear()

    out.write("</tv>\n")

    if len(channels_written) < MIN_CHANNELS or programmes == 0:
        sys.exit(f"Refusing to write output: only {len(channels_written)} channels / "
                 f"{programmes} programmes found (is the source file valid?)")

    outp = Path(args.output)
    outp.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the file byte-identical when the guide itself has not changed
    with open(outp, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                                               mtime=0, compresslevel=9) as f:
        f.write(out.getvalue().encode("utf-8"))
    missing = sorted(set(cfg["map"]) - channels_written)
    print(f"Wrote {outp}: {len(channels_written)} channels, {programmes} programmes")
    if missing:
        print("Not found in the source guide this time:", ", ".join(missing))


if __name__ == "__main__":
    main()
