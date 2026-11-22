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
import copy
import gzip
import time
import io
import json
import re
import sys
import unicodedata
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


def open_source(src, tries=3):
    """Read the M3UPT guide (URL or file). Downloads are retried a few times before giving up."""
    for attempt in range(1, tries + 1):
        try:
            if src.startswith(("http://", "https://")):
                req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (epg-pt-remap)"})
                data = urllib.request.urlopen(req, timeout=120).read()
            else:
                data = Path(src).read_bytes()
            if data[:2] == b"\x1f\x8b":  # gzip magic number
                data = gzip.decompress(data)
            if b"<tv" not in data[:2000]:
                raise ValueError("not an XMLTV file")
            return io.BytesIO(data)
        except Exception as exc:
            print(f"M3UPT guide: attempt {attempt} of {tries} failed ({exc})")
            if attempt == tries or not src.startswith(("http://", "https://")):
                raise
            time.sleep(30 * attempt)


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


# ---------------------------------------------------------------- MEO gap filler
# Where M3UPT only says "Informação Não Disponibilizada Pelo Canal" (or is empty), ask MEO's
# public TV guide (the one meo.pt uses) what is on in that slot. Everything else is untouched.

MEO_API = "https://meogouser.apps.meo.pt/Services/GridTv/GridTv.svc/GetLiveChannelProgramsByDate"
MEO_HEADERS = {
    "Accept": "*/*",
    "Origin": "https://www.meo.pt",
    "Referer": "https://www.meo.pt/",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/144.0.0.0 Mobile Safari/537.36",
}
GAP_TITLES = {"", "informacaonaodisponibilizadapelocanal", "direto", "semtitulo"}
MEO_MAX_REQUESTS = 80     # keep the load on MEO small
NO_INFO = "Informação Não Disponibilizada Pelo Canal"
GAP_HORIZON = timedelta(days=3)


def title_key(title):
    s = unicodedata.normalize("NFKD", title or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]", "", s)


def is_gap(title):
    return title_key(title) in GAP_TITLES


def find_gaps(src, targets, codes, now):
    """First pass over the guide: 'no information' slots per provider id, now to 3 days ahead."""
    gaps = defaultdict(set)
    for _e, el in ET.iterparse(src, events=("end",)):
        if el.tag == "programme":
            ids = [p for p in targets.get(el.get("channel"), ()) if p in codes]
            if ids and is_gap(el.findtext("title")):
                try:
                    s, e = parse_xmltv_time(el.get("start")), parse_xmltv_time(el.get("stop"))
                except (ValueError, TypeError):
                    s = e = None
                if s and e and e > now and s < now + GAP_HORIZON:
                    for pid in ids:
                        gaps[pid].add((s, e))
        if el.tag in ("programme", "channel"):
            el.clear()
    src.seek(0)
    return gaps


def fetch_meo(gaps, codes, local_dir=None):
    """Download MEO listings only for the channel/days that have gaps: {code: [programmes]}."""
    from zoneinfo import ZoneInfo
    lisbon = ZoneInfo("Europe/Lisbon")
    wanted = set()
    for pid, slots in gaps.items():
        for s, e in slots:
            d, last = s.astimezone(lisbon).date(), e.astimezone(lisbon).date()
            while d <= last:
                wanted.add((codes[pid], d))
                d += timedelta(days=1)
    result, fails, done = defaultdict(dict), 0, 0
    for code, d in sorted(wanted, key=lambda x: (x[1], x[0])):
        if done >= MEO_MAX_REQUESTS:
            print(f"MEO: stopped after {MEO_MAX_REQUESTS} requests")
            break
        done += 1
        try:
            if local_dir:   # testing: a missing file means "MEO has nothing"
                f = Path(local_dir) / f"{code}_{d.isoformat()}.json"
                raw = f.read_text(encoding="utf-8") if f.exists() else '{"Result": []}'
            else:
                q = urllib.parse.urlencode({"callLetter": code, "date": d.isoformat(),
                                            "userAgent": "IPTV_OFR_GTV"})
                req = urllib.request.Request(f"{MEO_API}?{q}", headers=MEO_HEADERS)
                raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8")
            items = json.loads(raw).get("Result") or []
        except Exception as exc:
            fails += 1
            if fails >= 3 and not result:
                print(f"MEO guide not reachable ({exc}); gaps stay as they are")
                return {}
            continue
        for it in items:
            try:
                start = datetime.fromisoformat(it["StartDate"]).replace(tzinfo=lisbon)
                stop = datetime.fromisoformat(it["EndDate"]).replace(tzinfo=lisbon)
            except (KeyError, TypeError, ValueError):
                continue
            title = (it.get("Title") or "").strip()
            if stop <= start or is_gap(title):
                continue
            result[code][(start, title)] = {"start": start.astimezone(timezone.utc),
                                            "stop": stop.astimezone(timezone.utc),
                                            "title": title,
                                            "desc": (it.get("Synopsis") or "").strip()}
    return {code: sorted(v.values(), key=lambda p: p["start"]) for code, v in result.items()}


def fill_gaps(gaps, codes, meo):
    """Gap slots MEO can fill -> (set of (pid, start, stop) to drop, {pid: [programmes] to add})."""
    drop, add = set(), defaultdict(dict)
    for pid, slots in gaps.items():
        progs = meo.get(codes[pid], [])
        for s, e in slots:
            inside = [p for p in progs if p["start"] < e and p["stop"] > s]
            if not inside:
                continue
            drop.add((pid, s, e))
            cursor = s
            for p in inside:   # clip to the gap so nothing overlaps the neighbouring entries
                q = {**p, "start": max(p["start"], s), "stop": min(p["stop"], e)}
                if q["stop"] <= q["start"]:
                    continue
                if q["start"] > cursor:   # part of the gap MEO does not cover: keep it marked
                    add[pid][(cursor, NO_INFO)] = {"start": cursor, "stop": q["start"],
                                                   "title": NO_INFO, "desc": ""}
                add[pid][(q["start"], q["title"])] = q
                cursor = max(cursor, q["stop"])
            if cursor < e:
                add[pid][(cursor, NO_INFO)] = {"start": cursor, "stop": e, "title": NO_INFO, "desc": ""}
    merged = {}
    for pid, v in add.items():   # join pieces of one programme split across two gap slots
        out = []
        for p in sorted(v.values(), key=lambda p: p["start"]):
            if out and out[-1]["title"] == p["title"] and out[-1]["stop"] == p["start"]:
                out[-1] = {**out[-1], "stop": p["stop"]}
            else:
                out.append(p)
        merged[pid] = out
    return drop, merged


def meo_programme_xml(pid, p):
    el = ET.Element("programme", start=xmltv_time(p["start"]), stop=xmltv_time(p["stop"]),
                    channel=pid)
    ET.SubElement(el, "title", lang="pt").text = p["title"]
    if p["desc"]:
        ET.SubElement(el, "desc", lang="pt").text = p["desc"]
    return ET.tostring(el, encoding="unicode")


# ---------------------------------------------------------------- main

def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="local M3UPT file (.xml or .xml.gz); default: download")
    ap.add_argument("--dazn-input", help="saved DAZN schedule JSON (for testing)")
    ap.add_argument("--no-dazn", action="store_true", help="do not use DAZN's schedule")
    ap.add_argument("--meo-input", help="folder with saved MEO answers CODE_YYYY-MM-DD.json (testing)")
    ap.add_argument("--no-meo", action="store_true", help="do not fill gaps from MEO")
    ap.add_argument("--now", help="pretend it is this UTC time, e.g. 2026-10-17T12:00 (testing)")
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

    # If M3UPT cannot be read, rebuild from the guide published last time, so that the
    # DAZN schedule still gets refreshed instead of the whole update failing.
    fallback = False
    try:
        src = open_source(args.input or cfg["source_url"])
    except Exception:
        previous = Path(args.output)
        if not previous.exists():
            raise
        print("M3UPT guide not available this time; reusing the last published guide "
              "(DAZN is still refreshed)")
        src = open_source(str(previous))
        targets = defaultdict(list, {pid: [pid] for pid in cfg["map"]})
        fallback = True
    now = (datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc) if args.now
           else datetime.now(timezone.utc))
    too_old = now - timedelta(days=1)
    extra_names = cfg.get("names", {})   # provider id -> extra display names to show in TiviMate

    # MEO gap filler: never allowed to break the build
    codes = {} if args.no_meo else cfg.get("meo", {})
    meo_drop, meo_add = set(), {}
    if codes:
        try:
            gaps = find_gaps(src, targets, codes, now)
            if gaps:
                meo_drop, meo_add = fill_gaps(gaps, codes, fetch_meo(gaps, codes, args.meo_input))
            print(f"MEO gap filler: {sum(len(v) for v in gaps.values())} empty slots on "
                  f"{len(gaps)} channels; {len(meo_drop)} filled with "
                  f"{sum(len(v) for v in meo_add.values())} MEO programmes"
                  + (f" ({', '.join(sorted(meo_add))})" if meo_add else ""))
        except Exception as exc:
            print(f"MEO gap filler skipped this time ({exc})")
            meo_drop, meo_add = set(), {}
            src.seek(0)

    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<tv generator-info-name="epg-pt-remap">\n')
    channels_written = set()
    programmes = replaced = 0

    for _event, el in ET.iterparse(src, events=("end",)):
        if el.tag == "channel":
            for pid in targets.get(el.get("id"), ()):
                if pid in channels_written:
                    continue
                ch = copy.deepcopy(el)
                ch.set("id", pid)
                have = {(d.text or "").strip() for d in ch.findall("display-name")}
                for name in extra_names.get(pid, []):
                    if name not in have:
                        # insert after the existing names, before icons
                        pos = len(ch.findall("display-name"))
                        new = ET.Element("display-name")
                        new.text = name
                        ch.insert(pos, new)
                out.write(ET.tostring(ch, encoding="unicode").strip() + "\n")
                channels_written.add(pid)
            el.clear()
        elif el.tag == "programme":
            ids = targets.get(el.get("channel"), ())
            if ids:
                try:
                    s, e = parse_xmltv_time(el.get("start")), parse_xmltv_time(el.get("stop"))
                except (ValueError, TypeError):
                    s = e = None
                if fallback and e and e < too_old:
                    el.clear()
                    continue   # drop old programmes so a reused guide does not keep growing
                for pid in ids:
                    span = dazn_span.get(pid)
                    if span and s and e and s < span[1] and e > span[0]:
                        replaced += 1   # DAZN's own data covers this slot
                        continue
                    if (pid, s, e) in meo_drop:
                        continue        # empty slot that MEO fills below
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
    meo_count = 0
    for pid, progs in meo_add.items():
        if pid in channels_written:
            for p in progs:
                out.write(meo_programme_xml(pid, p) + "\n")
                meo_count += 1
    programmes += meo_count
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
          f"({dazn_count} from DAZN, replacing {replaced} M3UPT entries; {meo_count} from MEO)")
    missing = sorted(set(cfg["map"]) - channels_written)
    if missing:
        print("Not found in the source guide this time:", ", ".join(missing))


if __name__ == "__main__":
    main()
