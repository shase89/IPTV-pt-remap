#!/usr/bin/env python3
"""Compare Portuguese TV-guide sources against your channel list and today's guide.

Commands (all paths relative to the repository root):
  conf    NAME LIST_XML CONF     match your channels to the source's channel list, write a grabber config
  chlist  NAME                   print the matched source channel ids, comma separated
  stats   NAME PROG_XML          measure coverage and quality of what the source returned
  summary                        print the comparison of every source measured so far
Set NAME to 'baseline' (with stats) to measure the guide you use today (docs/epg.xml.gz).
"""
import difflib
import gzip
import json
import os
import re
import statistics
import sys
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

WORK = os.environ.get("WORK", "work")
PT_FILE = os.environ.get("PT_FILE", "pt_channels.json")
BASELINE = os.environ.get("BASELINE", "docs/epg.xml.gz")
SPORTS = re.compile(r"sport|dazn|eleven|benfica|sporting|abola|canal11|eurosport|fueltv")
GENERIC_TITLES = {"", "direto", "live", "aovivo", "semprogramacao", "sememissao", "programacao",
                  "semtitulo", "notitle", "closed", "off", "transmissao",
                  "informacaonaodisponibilizadapelocanal"}
EPISODE_CODE = re.compile(r"^\d{4}\.S\d+\s*E\d+", re.I)
VERSUS = re.compile(r"\s(x|vs\.?)\s", re.I)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"^\s*pt\s*:", " ", s)
    s = re.sub(r"\b(fhd|hd|sd|hevc|uhd|4k|meo|nos|vodafone)\b", " ", s)
    return re.sub(r"[^a-z0-9+]", "", s)


def parse_time(s):
    s = s.strip()
    tz = timezone.utc
    m = re.search(r"([+-])(\d{2})(\d{2})\s*$", s[14:])
    if m:
        sign = 1 if m.group(1) == "+" else -1
        tz = timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))
    return datetime.strptime(s[:14], "%Y%m%d%H%M%S").replace(tzinfo=tz)


def open_xml(path):
    return gzip.open(path, "rb") if path.endswith(".gz") else open(path, "rb")


def read_channels(path):
    out = {}
    with open_xml(path) as f:
        for _e, el in ET.iterparse(f, events=("end",)):
            if el.tag == "channel":
                out[el.get("id")] = [(d.text or "").strip() for d in el.findall("display-name")]
                el.clear()
    return out


def wanted_names():
    """provider id -> set of normalised names (playlist names + the name used in today's guide)."""
    pt = json.load(open(PT_FILE, encoding="utf-8"))
    base = read_channels(BASELINE) if os.path.exists(BASELINE) else {}
    res = {}
    for pid, names in pt.items():
        s = {norm(n) for n in names}
        for n in base.get(pid, []):
            s.add(norm(n))
        for n in list(s):  # DAZN 1 may be listed as Eleven Sports 1 by some sources
            m = re.fullmatch(r"dazn(\d)", n)
            if m:
                s.add(f"elevensports{m.group(1)}")
        res[pid] = {n for n in s if n}
    return pt, res


def cmd_conf(name, list_xml, conf):
    os.makedirs(WORK, exist_ok=True)
    pt, wanted = wanted_names()
    src = read_channels(list_xml)
    src_norm = {cid: {norm(n) for n in names if n} for cid, names in src.items()}
    matches, how = {}, {}
    for pid, wn in wanted.items():
        hit = next((cid for cid, sn in src_norm.items() if wn & sn), None)
        if hit:
            matches[pid], how[pid] = hit, "exact"
            continue
        best, best_r = None, 0.0
        for cid, sn in src_norm.items():
            for a in wn:
                for b in sn:
                    r = difflib.SequenceMatcher(None, a, b).ratio()
                    if r > best_r:
                        best, best_r = cid, r
        if best and best_r >= 0.9:
            matches[pid], how[pid] = best, f"similar({best_r:.2f})"
    json.dump({"matches": matches, "how": how, "source_channels": len(src)},
              open(f"{WORK}/{name}.matches.json", "w"), indent=1)
    with open(conf, "w", encoding="utf-8") as f:
        for cid in sorted(set(matches.values())):
            f.write(f"channel={cid}\n")
    print(f"{name}: the source lists {len(src)} channels; {len(matches)} of your {len(pt)} "
          f"Portuguese channels found in it")
    missing = [p for p in pt if p not in matches]
    print("not found:", ", ".join(missing) if missing else "none")


def cmd_chlist(name):
    m = json.load(open(f"{WORK}/{name}.matches.json"))["matches"]
    print(",".join(sorted(set(m.values()))))


def classify(title):
    t = norm(title)
    return t in GENERIC_TITLES or bool(EPISODE_CODE.match(title.strip()))


def cmd_stats(name, prog_xml):
    os.makedirs(WORK, exist_ok=True)
    pt, _w = wanted_names()
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=7)
    if name == "baseline":
        rev = {pid: pid for pid in pt}
    else:
        m = json.load(open(f"{WORK}/{name}.matches.json"))["matches"]
        rev = {}
        for pid, cid in m.items():
            rev.setdefault(cid, []).append(pid)
        rev = {cid: pids for cid, pids in rev.items()}
    per = {}
    detail = {}
    with open_xml(prog_xml) as f:
        for _e, el in ET.iterparse(f, events=("end",)):
            if el.tag != "programme":
                continue
            ch = el.get("channel")
            try:
                s, e = parse_time(el.get("start")), parse_time(el.get("stop") or el.get("start"))
            except Exception:
                el.clear()
                continue
            title = (el.findtext("title") or "").strip()
            desc = (el.findtext("desc") or "").strip()
            pids = [ch] if name == "baseline" else rev.get(ch, [])
            for pid in pids:
                if pid not in pt:
                    continue
                if e >= now and s <= horizon:
                    st = per.setdefault(pid, {"n": 0, "desc": 0, "generic": 0, "versus": 0, "last": None})
                    st["n"] += 1
                    st["desc"] += bool(desc)
                    st["generic"] += classify(title)
                    st["versus"] += bool(VERSUS.search(title))
                    st["last"] = max(st["last"], e) if st["last"] else e
                if re.search(r"(dazn|eleven)", pid) or pid.startswith("elevensports"):
                    if now - timedelta(hours=2) <= e and s <= now + timedelta(hours=8):
                        detail.setdefault(pid, []).append((s, e, title))
            el.clear()
    out = {}
    for pid, st in per.items():
        out[pid] = {**{k: st[k] for k in ("n", "desc", "generic", "versus")},
                    "hours": round((st["last"] - now).total_seconds() / 3600, 1)}
    json.dump(out, open(f"{WORK}/{name}.stats.json", "w"), indent=1)
    print(f"{name}: {len(out)} of your {len(pt)} Portuguese channels have programmes in the next 7 days")
    print(f"\nDAZN channels, from 2 h ago to 8 h ahead (times in UTC; now {now:%d %H:%M}):")
    if not detail:
        print("  nothing found for the DAZN channels")
    for pid in sorted(detail):
        print(f"  [{pid}]")
        for s, e, t in sorted(detail[pid])[:12]:
            print(f"    {s.astimezone(timezone.utc):%d %H:%M}-{e.astimezone(timezone.utc):%H:%M}  {t}")


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "-"


def cmd_summary():
    pt, _w = wanted_names()
    names = [n[:-len(".stats.json")] for n in sorted(os.listdir(WORK)) if n.endswith(".stats.json")]
    names.sort(key=lambda n: (n != "baseline", n))
    data = {n: json.load(open(f"{WORK}/{n}.stats.json")) for n in names}
    print(f"COMPARISON - {len(pt)} Portuguese channels in your playlist, next 7 days\n")
    head = f"{'source':<22}{'channels':>9}{'median days':>12}{'with desc':>10}{'generic':>9}{'sports: named':>15}{'sports: generic':>16}"
    print(head)
    print("-" * len(head))
    for n in names:
        d = data[n]
        tot = sum(v["n"] for v in d.values())
        sp = [v for k, v in d.items() if SPORTS.search(k)]
        spn = sum(v["n"] for v in sp)
        days = statistics.median([v["hours"] for v in d.values()]) / 24 if d else 0
        print(f"{n:<22}{len(d):>9}{days:>12.1f}{pct(sum(v['desc'] for v in d.values()), tot):>10}"
              f"{pct(sum(v['generic'] for v in d.values()), tot):>9}"
              f"{pct(sum(v['versus'] for v in sp), spn):>15}{pct(sum(v['generic'] for v in sp), spn):>16}")
    print("\nchannels = how many of your channels have data; median days = how far ahead the guide goes;")
    print("with desc = programmes that have a description; generic = empty, '(Direto)' or episode-code titles;")
    print("sports named = sports programmes that name a match (A x B); sports generic = sports slots with no real title.")
    others = [n for n in names if n != "baseline"]
    if "baseline" in data and others:
        print("\nCHANNELS EACH SOURCE HAS THAT TODAY'S GUIDE DOES NOT:")
        for n in others:
            extra = sorted(set(data[n]) - set(data["baseline"]))
            print(f"  {n}: {', '.join(extra) if extra else 'none'}")
        print("\nCHANNELS TODAY'S GUIDE HAS THAT THE SOURCE DOES NOT:")
        for n in others:
            lost = sorted(set(data["baseline"]) - set(data[n]))
            print(f"  {n}: {', '.join(lost) if lost else 'none'}")
    print("\nNO SOURCE HAS:", ", ".join(sorted(set(pt) - set().union(*[set(d) for d in data.values()]))) or "none")
    print("\nPER CHANNEL (programmes / named matches / generic titles):")
    print(f"{'channel':<28}" + "".join(f"{n[:18]:<20}" for n in names))
    for pid in pt:
        row = f"{pid:<28}"
        for n in names:
            v = data[n].get(pid)
            cell = "%d/%d/%d" % (v["n"], v["versus"], v["generic"]) if v else "-"
            row += f"{cell:<20}"
        print(row)


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 4 and a[0] == "conf":
        cmd_conf(a[1], a[2], a[3])
    elif len(a) == 2 and a[0] == "chlist":
        cmd_chlist(a[1])
    elif len(a) == 3 and a[0] == "stats":
        cmd_stats(a[1], a[2])
    elif len(a) == 1 and a[0] == "summary":
        cmd_summary()
    else:
        sys.exit(__doc__)
