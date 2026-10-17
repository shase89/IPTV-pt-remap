#!/usr/bin/env python3
"""Build the Portuguese guide for TiviMate.

1. Downloads the M3UPT XMLTV guide, keeps only the channels in mapping.json and renames
   them to the IDs your provider's playlist uses.
2. Downloads DAZN's own Portuguese live-TV schedule (the one dazn.com shows) and uses it
   for DAZN 1-5, so live matches are named. If DAZN cannot be reached, the M3UPT data
   for those channels is kept, so the guide never ends up worse than before.

Usage:
    python3 build_epg.py                          # normal run (downloads both sources)
    python3 build_epg.py --input epg.xml.gz       # use a local copy of the M3UPT guide
    python3 build_epg.py --dazn-input dazn.json   # use a saved DAZN schedule (testing)
    python3 build_epg.py --no-dazn                # M3UPT only
"""
import argparse
import gzip
import io
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

MIN_CHANNELS = 50  # refuse to publish a guide that is clearly broken

# DAZN's public live-TV schedule, as used by dazn.com (same source as the iptv-org/epg project)
DAZN_API = "https://rail-router.discovery.indazn.com/eu/v10/Rail"
DAZN_DEFAULT = {
    "country": "pt",
    "language": "pt",
    # your provider id -> DAZN channel name (asset id is a fallback if names change)
    "channels": {
        "elevensports1.pt": {"name": "DAZN 1", "asset": "192nxdhuxe0xb1nxzvjrm68ho8"},
        "elevensports2.pt": {"name": "DAZN 2", "asset": "1o4twnocva6uwz9121szkcqly"},
        "elevensports3.pt": {"name": "DAZN 3", "asset": "29l8c8txra9q1gmicmku13nhw"},
        "elevensports4.pt": {"name": "DAZN 4", "asset": "gpg12e28xrnbz5mmgjrjnu99"},
        "elevensports5.pt": {"name": "DAZN 5", "asset": "1xi1dh83i1tl018kl25a3phsr2"},
    },
}


def open_source(src):
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (epg-pt-remap)"})
        data = urllib.request.urlopen(req, timeout=120).read()
    else:
        data = Path(src).read_bytes()
    if data[:2] == b"\x1f\x8b":  # gzip magic number
        data = gzip.decompress(data)
    return io.BytesIO(data)


def xmltv_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def parse_xmltv_time(s):
    s = (s or "").strip()
    tz = timezone.utc
    m = re.search(r"([+-])(\d{2})(\d{2})\s*$", s[14:])
    if m:
        sign = 1 if m.group(1) == "+" else -1
        tz = timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    return datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=tz)


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- DAZN

def fetch_dazn(cfg, local_file=None):
    """Return {provider_id: [programme dicts]} from DAZN's schedule, or {} on any failure."""
    try:
        if local_file:
            data = json.loads(Path(local_file).read_text(encoding="utf-8"))
        else:
            q = urllib.parse.urlencode({"platform": "web", "id": "Livetvschedule",
                                        "country": cfg["country"], "brand": "dazn",
                                        "languageCode": cfg["language"]})
            req = urllib.request.Request(f"{DAZN_API}?{q}", headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.dazn.com/",
                "User-Agent": "Mozilla/5.0 (epg-pt-remap)"})
            data = json.loads(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))
    except Exception as exc:  # network, HTTP or JSON problem: keep the M3UPT data
        print(f"DAZN schedule not available this time ({exc}); keeping M3UPT data for DAZN")
        return {}

    tiles = data.get("Tiles") if isinstance(data, dict) else None
    if not isinstance(tiles, list):
        print("DAZN schedule had an unexpected format; keeping M3UPT data for DAZN")
        return {}

    by_name = {(t.get("Title") or "").strip().lower(): t for t in tiles}
    by_asset = {t.get("AssetId"): t for t in tiles}
    result = {}
    for pid, ch in cfg["channels"].items():
        tile = by_name.get(ch["name"].lower()) or by_asset.get(ch.get("asset"))
        sched = (tile or {}).get("LinearSchedule") or {}
        items = [sched.get("Now"), sched.get("Next")] + list(sched.get("Later") or [])
        progs = []
        for it in items:
            if not it or not it.get("Start") or not it.get("End"):
                continue
            try:
                start, stop = parse_iso(it["Start"]), parse_iso(it["End"])
            except ValueError:
                continue
            if stop <= start:
                continue
            progs.append({"start": start, "stop": stop,
                          "title": (it.get("Title") or "").strip(),
                          "sub": (it.get("EpisodeTitle") or "").strip(),
                          "desc": (it.get("Description") or "").strip(),
                          "cats": [g.get("name") for g in (it.get("Genre") or [])
                                   if isinstance(g, dict) and g.get("name")]})
        if progs:
            result[pid] = clean_placeholders(sorted(progs, key=lambda p: p["start"]))
    found = ", ".join(f"{cfg['channels'][p]['name']}: {len(v)}" for p, v in result.items())
    print(f"DAZN schedule: {found or 'no programmes found'}")
    return result


PLACEHOLDER_TITLE = "Sem informação (DAZN)"


def is_placeholder(p):
    """DAZN fills unannounced slots with a one- or two-letter title such as 'D'."""
    return len(p["title"]) <= 2 and not p["sub"] and not p["desc"]


def clean_placeholders(progs):
    """Give placeholder slots a clear title and merge back-to-back ones into one block."""
    out = []
    for p in progs:
        if is_placeholder(p):
            p = {**p, "title": PLACEHOLDER_TITLE, "cats": []}
            prev = out[-1] if out else None
            if prev and prev["title"] == PLACEHOLDER_TITLE and prev["stop"] >= p["start"]:
                prev["stop"] = max(prev["stop"], p["stop"])
                continue
        out.append(p)
    return out


def dazn_title(p):
    title, sub = p["title"], p["sub"]
    if sub and title and sub.lower() not in title.lower():
        return f"{sub} ({title})"   # e.g. "Chelsea - Brighton (Premier League)"
    return title or sub or "DAZN"


def dazn_programme_xml(pid, p):
    el = ET.Element("programme", start=xmltv_time(p["start"]), stop=xmltv_time(p["stop"]),
                    channel=pid)
    ET.SubElement(el, "title", lang="pt").text = dazn_title(p)
    if p["sub"]:
        ET.SubElement(el, "sub-title", lang="pt").text = p["sub"]
    if p["desc"]:
        ET.SubElement(el, "desc", lang="pt").text = p["desc"]
    for c in dict.fromkeys(p["cats"]):
        ET.SubElement(el, "category", lang="en").text = c
    return ET.tostring(el, encoding="unicode")


# ---------------------------------------------------------------- main

def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="local M3UPT file (.xml or .xml.gz); default: download")
    ap.add_argument("--dazn-input", help="saved DAZN schedule JSON (for testing)")
    ap.add_argument("--no-dazn", action="store_true", help="do not use DAZN's schedule")
    ap.add_argument("--mapping", default=str(here / "mapping.json"))
    ap.add_argument("--output", default=str(here / "docs" / "epg.xml.gz"))
    args = ap.parse_args()

    cfg = json.loads(Path(args.mapping).read_text(encoding="utf-8"))
    dazn_cfg = cfg.get("dazn", DAZN_DEFAULT)

    # m3upt id -> every provider id that should get a copy of it
    targets = defaultdict(list)
    for provider_id, m3upt_id in cfg["map"].items():
        targets[m3upt_id].append(provider_id)

    dazn = {} if args.no_dazn else fetch_dazn(dazn_cfg, args.dazn_input)
    # time span DAZN covers per channel: M3UPT entries inside it are replaced
    dazn_span = {pid: (v[0]["start"], max(p["stop"] for p in v)) for pid, v in dazn.items()}

    src = open_source(args.input or cfg["source_url"])

    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<tv generator-info-name="epg-pt-remap">\n')
    channels_written = set()
    programmes = replaced = 0

    for _event, el in ET.iterparse(src, events=("end",)):
        if el.tag == "channel":
            for pid in targets.get(el.get("id"), ()):
                el.set("id", pid)
                out.write(ET.tostring(el, encoding="unicode").strip() + "\n")
                channels_written.add(pid)
                el.set("id", "")
            el.clear()
        elif el.tag == "programme":
            ids = targets.get(el.get("channel"), ())
            if ids:
                try:
                    s, e = parse_xmltv_time(el.get("start")), parse_xmltv_time(el.get("stop"))
                except (ValueError, TypeError):
                    s = e = None
                for pid in ids:
                    span = dazn_span.get(pid)
                    if span and s and e and s < span[1] and e > span[0]:
                        replaced += 1   # DAZN's own data covers this slot
                        continue
                    el.set("channel", pid)
                    out.write(ET.tostring(el, encoding="unicode").strip() + "\n")
                    programmes += 1
            el.clear()

    dazn_count = 0
    for pid, progs in dazn.items():
        if pid not in channels_written:  # channel missing from M3UPT: add it ourselves
            ch = ET.Element("channel", id=pid)
            ET.SubElement(ch, "display-name").text = dazn_cfg["channels"][pid]["name"]
            out.write(ET.tostring(ch, encoding="unicode") + "\n")
            channels_written.add(pid)
        for p in progs:
            out.write(dazn_programme_xml(pid, p) + "\n")
            dazn_count += 1
    out.write("</tv>\n")

    if len(channels_written) < MIN_CHANNELS or programmes + dazn_count == 0:
        sys.exit(f"Refusing to write output: only {len(channels_written)} channels / "
                 f"{programmes + dazn_count} programmes found (is the source file valid?)")

    outp = Path(args.output)
    outp.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the file byte-identical when the guide itself has not changed
    with open(outp, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                                               mtime=0, compresslevel=9) as f:
        f.write(out.getvalue().encode("utf-8"))
    print(f"Wrote {outp}: {len(channels_written)} channels, {programmes + dazn_count} programmes "
          f"({dazn_count} from DAZN, replacing {replaced} M3UPT entries)")
    missing = sorted(set(cfg["map"]) - channels_written)
    if missing:
        print("Not found in the source guide this time:", ", ".join(missing))


if __name__ == "__main__":
    main()
