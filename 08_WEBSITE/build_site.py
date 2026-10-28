# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow"]
# ///
"""Builds the public website from the pipeline outputs (stages 01-07). No downloads.

    uv run build_site.py            (from inside 08_WEBSITE)

Reads only files the earlier stages wrote, and writes everything into site/, which is the folder
GitHub Pages publishes. Every page is plain HTML, readable without JavaScript; the script in
site/assets only adds the map, the table filters and the result tabs.

Before writing anything it recomputes the headline numbers from verdicts.csv and checks them
against 06_VERDICTS/outputs/verdict_report.txt; if they disagree, it stops.

Preview locally:  cd site && python3 -m http.server   then open http://localhost:8000
(opening index.html directly does not load the map).
"""
from __future__ import annotations

import csv
import html
import json
import math
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = HERE / "src"
SITE = HERE / "site"

# ------------------------------------------------------------------ settings you may edit
AUTHOR = "Kristof Krebs"
REPO_URL = "https://github.com/chris-krebs/Satellite_Imagery_Corroboration"
TITLE = "Event corroboration with open-source data streams"
SUBTITLE = "OpenStreetMap, NASA FIRMS and Sentinel-2 imagery, tested on 100 past fires, explosions and strikes."
COVER_EVENT = "E017"    # Toropets: image tiles shown on the cover
IMG_MAX_W = 1430        # px, full images
THUMB_W = 720           # px, case thumbnails
JPEG_Q = 80
MAPLIBRE = "5.24.0"
MAPLIBRE_CSS_SRI = "sha384-uTttxo/aOKbdE5RlD/SPzSDoDmNvGlUYPjONi2MN/b7c9HPSvW07OIuyP7uL6jxK"
MAPLIBRE_JS_SRI = "sha384-5+cfbwT0iiub6VsQAdn6yz16nr6sDiQoHx6tm4O8OVYXHYOxcffFmCJBL0dgdvGp"

# ------------------------------------------------------------------ inputs
P = {
    "events": ROOT / "02_OSM_CHECK/data/events_reviewed.csv",
    "footprints": ROOT / "02_OSM_CHECK/data/site_footprints.geojson",
    "firms": ROOT / "04_FIRMS/outputs/firms_event_summary.csv",
    "heat": ROOT / "05_PERSISTENT_HEAT/outputs/heat_event_summary.csv",
    "dets": ROOT / "05_PERSISTENT_HEAT/outputs/labelled_detections.csv",
    "cells": ROOT / "05_PERSISTENT_HEAT/outputs/heat_cells.geojson",
    "thresholds": ROOT / "05_PERSISTENT_HEAT/data/frozen_thresholds.json",
    "verdicts": ROOT / "06_VERDICTS/outputs/verdicts.csv",
    "rules": ROOT / "06_VERDICTS/data/verdict_rules.json",
    "report": ROOT / "06_VERDICTS/outputs/verdict_report.txt",
    "s2": ROOT / "07_SENTINEL2/outputs/s2_summary.csv",
    "blind": ROOT / "07_SENTINEL2/outputs/blind_results.csv",
    "blind_key": ROOT / "07_SENTINEL2/outputs/blind_key.csv",
    "img_panels": ROOT / "07_SENTINEL2/outputs/compressed/panels",
    "img_blind": ROOT / "07_SENTINEL2/outputs/compressed/blind",
    "png_panels": ROOT / "07_SENTINEL2/outputs/panels",
    "png_blind": ROOT / "07_SENTINEL2/outputs/blind",
}

# ------------------------------------------------------------------ vocabulary
CATS = {
    "industrial_fire": "Industrial fire",
    "large_outdoor_fire": "Large outdoor fire",
    "port_ship_fire": "Port or ship fire",
    "small_or_indoor": "Small or indoor",
    "negative_control": "Control",
}
CAT_ORDER = ["industrial_fire", "large_outdoor_fire", "port_ship_fire", "small_or_indoor"]
SV = {  # site verdicts, in table order
    "corroborated": "Corroborated",
    "ambiguous_attribution": "Ambiguous attribution",
    "weak": "Weak",
    "not_observable": "Not observable",
    "not_corroborated": "Not corroborated",
    "not_assessable_no_site": "No site point",
}
CV = {"corroborated": "Corroborated", "not_corroborated": "Not corroborated"}
LABELS = {
    "active_fire": "active fire", "smoke": "smoke", "new_damage_or_scar": "new damage or burn scar",
    "nothing_visible": "nothing visible", "not_assessable": "not assessable",
}
S2_STATUS = {
    "ok": "Clear images before and after.",
    "no_clear_after": "No clear image within 30 days after.",
    "no_clear_before": "No clear image in the 45 days before.",
    "no_clear_scenes": "No clear image before or after.",
    "skipped_no_site_point": "Not run. No site point.",
    "skipped_position_too_vague": "Not run. Position too vague for a 10 m image (e.g. a ship at sea).",
}
SPLIT = {"test": "Test half", "calibration": "Calibration half"}

# ------------------------------------------------------------------ helpers
esc = lambda s: html.escape("" if s is None else str(s), quote=True)


def read_csv(p):
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def num(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def pct(x):
    return f"{round(100 * x):d}%"


def fmt_date(s):
    """'2024-09-18' or '2025-11-13T21:00Z' -> '18 Sep 2024' (+ ', 21:00 UTC')."""
    if not s:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?", s)
    if not m:
        return s
    d = datetime(int(m[1]), int(m[2]), int(m[3]))
    out = f"{d.day} {d.strftime('%b %Y')}"
    if m[4]:
        out += f", {m[4]}:{m[5]} UTC"
    return out


def hours(h):
    v = num(h)
    if v is None:
        return ""
    return f"{'+' if v >= 0 else '−'}{abs(v):.1f} h"


def ring(lon, lat, km, n=48):
    pts = []
    for i in range(n + 1):
        a = 2 * math.pi * i / n
        dlat = km / 110.574 * math.sin(a)
        dlon = km / (111.320 * math.cos(math.radians(lat))) * math.cos(a)
        pts.append([round(lon + dlon, 5), round(lat + dlat, 5)])
    return pts


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def links(sources):
    urls = re.findall(r"https?://[^\s;|,]+", sources or "")
    out = []
    for u in urls:
        host = re.sub(r"^www\.", "", u.split("/")[2])
        out.append(f'<a href="{esc(u)}" rel="noopener">{esc(host)}</a>')
    return out


def pill(v, kind="site"):
    label = (SV if kind == "site" else CV).get(v, v or "—")
    return f'<span class="pill v-{esc(v or "none")}">{esc(label)}</span>'


# Glue the last two words of every text block with a no-break space, so no paragraph, list item,
# caption or heading ends with a single word on its own line. The run after the space may only
# contain text and attribute-free tags (e.g. </a>), so a space inside a tag is never touched.
_WIDOW = re.compile(r" ((?:[^\s<>]|</?[a-z0-9]+>|<span class=\"nw\">)+)(</(?:p|li|dd|figcaption|h1|h2|h3|h4|summary)>)")
_HYPHEN_WORD = re.compile(r"\b([A-Za-z]+-(?:[A-Za-z]+|\d+))\b")   # Sentinel-2, open-source, ground-truth
_N_OF_M = re.compile(r"\b(\d+) (of|vs) (\d+)\b")                   # keep "0 of 5" on one line


def _fix_text(t):
    t = _N_OF_M.sub(r"\1&nbsp;\2&nbsp;\3", t)
    return _HYPHEN_WORD.sub(r'<span class="nw">\1</span>', t)


def no_widows(page):
    """Typography for the visible text only (never inside tags or the <head>)."""
    head_end = page.find("<body")
    body = re.sub(r">([^<]+)<", lambda m: ">" + _fix_text(m[1]) + "<", page[head_end:])
    return page[:head_end] + _WIDOW.sub(r"&nbsp;\1\2", body)


# ------------------------------------------------------------------ load
def load():
    for k, p in P.items():
        if not p.exists() and not k.startswith(("img_", "png_")):
            sys.exit(f"missing input: {p}")
    ev = {r["event_id"]: r for r in read_csv(P["events"])}
    ver = {r["event_id"]: r for r in read_csv(P["verdicts"])}
    firms = {r["event_id"]: r for r in read_csv(P["firms"])}
    heat = {r["event_id"]: r for r in read_csv(P["heat"])}
    s2 = {r["event_id"]: r for r in read_csv(P["s2"])}
    blind = {r["event_id"]: r for r in read_csv(P["blind"])}
    bkey = {r["event_id"]: r["blind_id"] for r in read_csv(P["blind_key"])}
    dets = defaultdict(list)
    for r in read_csv(P["dets"]):
        if r["window"] == "event":
            dets[r["event_id"]].append(r)
    cells = json.loads(P["cells"].read_text(encoding="utf-8"))
    foot = json.loads(P["footprints"].read_text(encoding="utf-8"))
    rules = json.loads(P["rules"].read_text(encoding="utf-8"))
    thr = json.loads(P["thresholds"].read_text(encoding="utf-8"))
    report = P["report"].read_text(encoding="utf-8")
    assert set(ev) == set(ver), "events and verdicts list different events"
    return dict(ev=ev, ver=ver, firms=firms, heat=heat, s2=s2, blind=blind, bkey=bkey, dets=dets,
                cells=cells, foot=foot, rules=rules, thr=thr, report=report)


# ------------------------------------------------------------------ numbers
def is_control(v):
    return v["category"] == "negative_control"


def tally(rows):
    c = Counter(r["site_verdict"] for r in rows)
    obs = sum(c[k] for k in ("corroborated", "ambiguous_attribution", "weak", "not_corroborated"))
    city = sum(r["city_verdict"] == "corroborated" for r in rows)
    return dict(c=c, k=c["corroborated"], obs=obs, n=len(rows), city=city)


def compute(D):
    V = list(D["ver"].values())
    out = {}
    for split in ("all", "test", "calibration"):
        rows = [v for v in V if split == "all" or v["split"] == split]
        inc = [v for v in rows if not is_control(v)]
        ctl = [v for v in rows if is_control(v)]
        cats = {c: tally([v for v in inc if v["category"] == c]) for c in CAT_ORDER}
        out[split] = dict(inc=tally(inc), ctl=tally(ctl), cats=cats)
    inc = [v for v in V if not is_control(v)]
    with_site = [v for v in inc if v["site_verdict"] != "not_assessable_no_site"]
    sy = lambda v: v["site_verdict"] == "corroborated"
    cy = lambda v: v["city_verdict"] == "corroborated"
    out["cross"] = dict(
        both=sum(sy(v) and cy(v) for v in with_site), site_only=sum(sy(v) and not cy(v) for v in with_site),
        city_only=sum(cy(v) and not sy(v) for v in with_site), neither=sum(not sy(v) and not cy(v) for v in with_site),
        n=len(with_site), city_on_site_events=sum(cy(v) for v in with_site),
        nosite=len(inc) - len(with_site), nosite_city=sum(cy(v) for v in inc if v not in with_site),
    )
    out["n_inc"], out["n_ctl"] = len(inc), len(V) - len(inc)
    return out


def check_against_report(N, report):
    """Stop if the numbers on the site would differ from verdict_report.txt."""
    problems = []
    for split, head in (("test", "SITE LEVEL, test"), ("calibration", "SITE LEVEL, calibration"), ("all", "SITE LEVEL, all")):
        sec = report.split(f"== {head} ==")[1].split("==")[0]
        m = re.search(r"events corroborated \(where observable\): (\d+)/(\d+)", sec)
        c = re.search(r"controls 'corroborated' \(false alarms\):\s+(\d+)/(\d+)", sec)
        t = N[split]
        if (int(m[1]), int(m[2])) != (t["inc"]["k"], t["inc"]["obs"]):
            problems.append(f"{split} events {m[1]}/{m[2]} vs {t['inc']['k']}/{t['inc']['obs']}")
        if (int(c[1]), int(c[2])) != (t["ctl"]["k"], t["ctl"]["n"]):
            problems.append(f"{split} controls {c[1]}/{c[2]} vs {t['ctl']['k']}/{t['ctl']['n']}")
        lo, hi = wilson(t["inc"]["k"], t["inc"]["obs"])
        if f"95% CI {pct(lo)}-{pct(hi)}" not in sec:
            problems.append(f"{split} CI {pct(lo)}-{pct(hi)} not in report")
    m = re.search(r"events\s+city-level corroborated: (\d+)/(\d+)", report)
    if (int(m[1]), int(m[2])) != (N["all"]["inc"]["city"], N["n_inc"]):
        problems.append("city-level events")
    x = N["cross"]
    m = re.search(r"site yes & city yes (\d+), site yes & city no (\d+), site no & city yes (\d+), both no (\d+)", report)
    if tuple(map(int, m.groups())) != (x["both"], x["site_only"], x["city_only"], x["neither"]):
        problems.append("site/city cross-table")
    for c in CAT_ORDER:
        m = re.search(rf"{c}\s+(\d+)/(\d+)", report)
        t = N["all"]["cats"][c]
        if (int(m[1]), int(m[2])) != (t["k"], t["obs"]):
            problems.append(f"category {c}")
    if problems:
        sys.exit("numbers differ from verdict_report.txt: " + "; ".join(problems))


# ------------------------------------------------------------------ images
# Panel tiles inside a 1430 x 946 blind image (matplotlib layout of 07_SENTINEL2): left, top, right, bottom
TILES = {"tc_before": (39, 104, 484, 487), "tc_after": (503, 104, 947, 487),
         "sw_before": (39, 503, 484, 886), "sw_after": (503, 503, 947, 886)}


def make_images(D):
    """Web copies: img/blind/E017.jpg (what the labeller saw), img/panels/E017.jpg (with FIRMS + name),
    img/thumb/E017.jpg, and the cover tiles. Only re-encodes when the source is newer than the copy."""
    from PIL import Image

    have = {}
    jobs = []
    for eid in D["ver"]:
        bid = D["bkey"].get(eid)
        src_p = [P["img_panels"] / f"{eid}.jpeg", P["png_panels"] / f"{eid}.png"]
        src_b = [P["img_blind"] / f"{bid}.jpeg", P["png_blind"] / f"{bid}.png"] if bid else []
        sp = next((p for p in src_p if p.exists()), None)
        sb = next((p for p in src_b if p.exists()), None)
        if sp:
            jobs.append((sp, SITE / "img/panels" / f"{eid}.jpg", IMG_MAX_W, None))
        if sb:
            jobs.append((sb, SITE / "img/blind" / f"{eid}.jpg", IMG_MAX_W, None))
            jobs.append((sb, SITE / "img/thumb" / f"{eid}.jpg", THUMB_W, None))
            if eid == COVER_EVENT:
                for name, box in TILES.items():
                    jobs.append((sb, SITE / "img/cover" / f"{name}.jpg", 9999, box))
        have[eid] = dict(panel=bool(sp), blind=bool(sb))
    made = 0
    for src, dst, w, box in jobs:
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        im = Image.open(src).convert("RGB")
        if box:
            if im.size != (1430, 946):
                im = im.resize((1430, 946), Image.LANCZOS)
            im = im.crop(box)
        if im.width > w:
            im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
        im.save(dst, "JPEG", quality=JPEG_Q + (8 if box else 0), optimize=True, progressive=True)
        made += 1
    return have, made


# ------------------------------------------------------------------ data files for the map and for download
def write_data(D, have):
    ev, ver = D["ev"], D["ver"]
    data = SITE / "data"
    data.mkdir(parents=True, exist_ok=True)
    sites = {}
    events = {}
    rank = ["corroborated", "ambiguous_attribution", "weak", "not_corroborated", "not_observable"]
    rings, cityp, offsets = [], [], []
    for eid in sorted(ev):
        e, v = ev[eid], ver[eid]
        lat, lon = num(e["lat_site"]), num(e["lon_site"])
        clat, clon = num(e["lat_coarse"]), num(e["lon_coarse"])
        h = D["heat"].get(eid, {})
        dl = D["dets"].get(eid, [])
        b = D["blind"].get(eid, {})
        events[eid] = dict(
            id=eid, site=e["site_id"] if lat is not None else None, name=e["place_name"], cat=v["category"],
            date=fmt_date(e["event_time_utc"]), split=v["split"], gt=v["ground_truth"], sv=v["site_verdict"],
            cv=v["city_verdict"], reason=v["site_reason"], ev=v["site_evidence"],
            cpass=v["city_overpasses_in_window"], cexp=v["city_expected_from_baseline"],
            sig=int(num(h.get("n_site_signal"), 0) or 0), bg=int(num(h.get("n_background"), 0) or 0),
            ndet=len(dl), label=b.get("label", ""), img=have[eid]["blind"],
            city=[clon, clat] if clat is not None else None, place=e["coarse_place"],
            off=num(e["coarse_offset_km"]),
        )
        if clat is not None:
            rings.append({"type": "Feature", "properties": {"e": eid, "nosite": lat is None},
                          "geometry": {"type": "Polygon", "coordinates": [ring(clon, clat, D["rules"]["city_radius_km"])]}})
            cityp.append({"type": "Feature", "properties": {"e": eid}, "geometry": {"type": "Point", "coordinates": [clon, clat]}})
        if lat is not None:
            s = sites.setdefault(e["site_id"], dict(id=e["site_id"], name=e["place_name"], lon=lon, lat=lat, events=[]))
            s["events"].append(eid)
            if clat is not None:
                offsets.append({"type": "Feature", "properties": {"e": eid, "km": round(haversine(lat, lon, clat, clon), 1)},
                                "geometry": {"type": "LineString", "coordinates": [[lon, lat], [clon, clat]]}})
    outlined = {f["properties"]["site_id"] for f in D["foot"]["features"]}
    feats = []
    for s in sites.values():
        inc = [events[x] for x in s["events"] if events[x]["cat"] != "negative_control"]
        best = min((rank.index(x["sv"]) for x in inc if x["sv"] in rank), default=len(rank) - 1)
        s["v"] = rank[best]
        s["outline"] = s["id"] in outlined
        feats.append({"type": "Feature", "properties": {"site": s["id"], "name": s["name"], "v": s["v"], "n": len(s["events"])},
                      "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]}})
    nosite = [{"type": "Feature", "properties": {"e": x["id"], "name": x["name"], "cv": x["cv"]},
               "geometry": {"type": "Point", "coordinates": x["city"]}}
              for x in events.values() if x["site"] is None and x["city"]]
    fc = lambda f: {"type": "FeatureCollection", "features": f}
    mapdata = dict(sites=fc(feats), nosite=fc(nosite), rings=fc(rings), city=fc(cityp), offsets=fc(offsets),
                   events=events, siteinfo={k: dict(name=s["name"], events=s["events"], outline=s["outline"]) for k, s in sites.items()},
                   city_km=D["rules"]["city_radius_km"])
    (data / "map.json").write_text(json.dumps(mapdata, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # detections per event, compact: [lon, lat, label, hours, frp, satellite, utc]
    det = {eid: [[num(r["longitude"]), num(r["latitude"]), r["label"], num(r["hours_from_event"]), num(r["frp_mw"]),
                  r["satellite"], r["acq_utc"]] for r in rows] for eid, rows in D["dets"].items()}
    (data / "detections.json").write_text(json.dumps(det, separators=(",", ":")), encoding="utf-8")

    cells = D["cells"]
    for f in cells["features"]:
        f["geometry"]["coordinates"] = [[[round(x, 5), round(y, 5)] for x, y in r] for r in f["geometry"]["coordinates"]]
    (data / "heat_cells.geojson").write_text(json.dumps(cells, separators=(",", ":")), encoding="utf-8")

    keep = ("site_id", "osm", "osm_name", "osm_url", "area_km2")
    outl = {"type": "FeatureCollection",
            "license": "ODbL 1.0. Contains information from OpenStreetMap, made available under the Open Database Licence: https://opendatacommons.org/licenses/odbl/1-0/. © OpenStreetMap contributors.",
            "features": [{"type": "Feature", "properties": {k: f["properties"].get(k) for k in keep}, "geometry": f["geometry"]}
                         for f in D["foot"]["features"]]}
    (data / "site_outlines_odbl.geojson").write_text(json.dumps(outl, ensure_ascii=False), encoding="utf-8")

    cols = ["event_id", "site_id", "split", "category", "ground_truth", "place_name", "event_time_utc", "time_precision",
            "lat_site", "lon_site", "site_coord_basis", "lat_coarse", "lon_coarse", "coarse_place", "coarse_offset_km",
            "site_verdict", "site_evidence", "site_reason", "city_verdict", "city_overpasses_in_window",
            "city_expected_from_baseline", "city_p_value", "visual_label", "visual_seen_before", "circular_risk", "sources"]
    with open(data / "events.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for eid in sorted(ev):
            row = {**ev[eid], **ver[eid]}
            w.writerow({c: row.get(c, "") for c in cols})
    with open(data / "fire_detections.csv", "w", encoding="utf-8", newline="") as f:
        cols = ["event_id", "site_id", "satellite", "product", "acq_utc", "hours_from_event", "latitude", "longitude",
                "frp_mw", "confidence", "daynight", "dist_site_km", "label", "signal"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for eid in sorted(D["dets"]):
            for r in D["dets"][eid]:
                w.writerow({c: r.get(c, "") for c in cols})
    shutil.copy(P["rules"], data / "verdict_rules.json")
    shutil.copy(P["thresholds"], data / "frozen_thresholds.json")
    shutil.copy(P["report"], data / "verdict_report.txt")
    shutil.copy(SRC / "basemap-dark.json", data / "basemap-dark.json")
    return mapdata


# ------------------------------------------------------------------ page shell
def head(title, desc, depth=0):
    up = "../" * depth
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<meta name="author" content="{esc(AUTHOR)}">
<meta name="theme-color" content="#000000">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="article">
<link rel="stylesheet" href="{up}assets/site.css">
"""


def masthead(depth=0):
    up = "../" * depth
    home = f"{up}index.html"
    return f"""<header class="masthead">
  <div class="wrap masthead-row">
    <p class="byline"><a href="{home}">Event corroboration</a> · {esc(AUTHOR)}</p>
    <nav aria-label="Sections">
      <a href="{home}#summary">Summary</a>
      <a href="{home}#results">Results</a>
      <a href="{home}#map-section">Map</a>
      <a href="{home}#method">Method</a>
      <a href="{home}#cases">Cases</a>
      <a href="{home}#limits">Limits</a>
      <a href="{home}#data">Data</a>
    </nav>
  </div>
</header>
"""


def footer(depth=0, built=""):
    up = "../" * depth
    return f"""<footer class="wrap foot">
  <p>{esc(AUTHOR)}. Built with the assistance of Claude Opus 5.5 (Anthropic), an AI model, for coding, data processing, source gathering and drafting. All output was reviewed by the author, who is responsible for the content. <a href="{up}index.html#ai-use">How AI was used</a>.</p>
  <p>Fire detections: NASA FIRMS (<a href="https://www.earthdata.nasa.gov/data/projects/lance/disclaimer">disclaimer</a>). Contains modified Copernicus Sentinel data 2021–2026. Map data and site outlines © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors (ODbL). ESA WorldCover. VersaTiles. Satellite base map © Esri, Maxar, Earthstar Geographics. Place coordinates: GeoNames (CC BY 4.0), Wikipedia. Last build {esc(built)}.</p>
</footer>
"""


# ------------------------------------------------------------------ index page
def stat(big, small, note=""):
    return f'<div class="stat"><p class="stat-big">{big}</p><p class="stat-label">{small}</p>{f"<p class=stat-note>{note}</p>" if note else ""}</div>'


def matrix(N, split):
    t = N[split]
    cols = list(SV)
    hdr = "".join(f'<th scope="col"><span class="pill v-{c}">{esc(SV[c])}</span></th>' for c in cols)

    def row(label, tl, cls=""):
        cells = "".join(f'<td class="{"zero" if tl["c"][c] == 0 else ""}">{tl["c"][c]}</td>' for c in cols)
        if tl["obs"]:
            lo, hi = wilson(tl["k"], tl["obs"])
            rate = f'<b>{tl["k"]} of {tl["obs"]}</b><span class="ci">{pct(lo)}–{pct(hi)}</span>'
        else:
            rate = "—"
        return f'<tr class="{cls}"><th scope="row">{label}</th>{cells}<td class="rate">{rate}</td><td class="rate"><b>{tl["city"]} of {tl["n"]}</b></td></tr>'
    body = "".join(row(CATS[c], t["cats"][c]) for c in CAT_ORDER)
    body += row("All incidents", t["inc"], "total")
    body += row("Controls <span class=muted>(ordinary days)</span>", t["ctl"], "controls")
    return f"""<div class="table-scroll"><table class="matrix">
<caption class="sr">Verdicts by category, {esc(split)} events</caption>
<thead><tr><th scope="col">Category</th>{hdr}<th scope="col" class="rate">Site level, of observable <span class="muted">(95% CI)</span></th><th scope="col" class="rate">City level, of all</th></tr></thead>
<tbody>{body}</tbody></table></div>"""


def event_table(D):
    rows = []
    for eid in sorted(D["ver"]):
        v, e = D["ver"][eid], D["ev"][eid]
        rows.append(
            f'<tr data-cat="{esc(v["category"])}" data-sv="{esc(v["site_verdict"])}" data-split="{esc(v["split"])}" data-cv="{esc(v["city_verdict"])}">'
            f'<td class="id"><a href="events/{eid}.html">{eid}</a></td>'
            f'<td class="place"><a href="events/{eid}.html">{esc(v["place_name"])}</a>'
            f'{" <span class=muted>(" + esc(v["ground_truth"]) + ")</span>" if v["ground_truth"] == "disputed" else ""}</td>'
            f'<td>{esc(CATS[v["category"]])}</td><td class="date">{esc(fmt_date(e["event_time_utc"]).split(",")[0])}</td>'
            f'<td>{pill(v["site_verdict"])}</td><td>{pill(v["city_verdict"], "city")}</td>'
            f'<td class="why">{esc(v["site_reason"])}</td>'
            f'<td class="actions"><a class="open-link" href="events/{eid}.html" aria-label="Evidence for {eid}">Details&nbsp;→</a>'
            f'<button type="button" class="to-map" data-event="{eid}" hidden>Map</button></td></tr>')
    opt = lambda d: "".join(f'<option value="{k}">{esc(l)}</option>' for k, l in d.items())
    return f"""<form class="filters" id="filters" aria-label="Filter events" hidden>
  <label>Category <select name="cat"><option value="">All</option>{opt(CATS)}</select></label>
  <label>Site verdict <select name="sv"><option value="">All</option>{opt(SV)}</select></label>
  <label>City verdict <select name="cv"><option value="">All</option>{opt(CV)}</select></label>
  <label>Half <select name="split"><option value="">Both</option>{opt(SPLIT)}</select></label>
  <p class="count" id="count" aria-live="polite"></p>
</form>
<div class="table-scroll events-scroll"><table class="events" id="events">
<thead><tr><th scope="col">ID</th><th scope="col">Place</th><th scope="col">Category</th><th scope="col">Date</th><th scope="col">Site verdict</th><th scope="col">City verdict</th><th scope="col">Why</th><th scope="col">Evidence</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


CASES = [
    # (group, event ids, title, text). Numbers checked against the pipeline outputs on 28 Oct 2026.
    ("works", ["E017"], "Toropets arsenal: a clear hit",
     "512 FIRMS detections passed the heat filter within the time window. The first was recorded 0.9 h before the reported main blast. "
     "The image taken a day later shows smoke over the depot and hot spots in the infrared. It was labelled active fire. "
     "Two caveats apply. The image had been seen before labelling. One ground-truth source, Wikipedia, itself cites FIRMS."),
    ("works", ["E006", "E077"], "Yaroslavl refinery: the flare filter at work",
     "Refineries produce heat every day. On the control date (E077), all 28 detections at the site matched the usual pattern. "
     "All were labelled background, and no false alarm was raised. On the night of the strike (E006), 14 of 15 detections were new or anomalous. The event was corroborated."),
    ("fails", ["E002"], "Volgograd refinery: the wrong fire",
     "The only FIRMS signal begins 71 h after the reported strike. By then, a second attack had hit the same refinery. "
     "Counting it would confirm the first event with the fire from the second. The attribution rule excludes it. "
     "The site was under cloud, so the verdict is not observable."),
    ("fails", ["E020", "E083"], "Garyville refinery: flares look like fire",
     "In shortwave infrared, refinery flares resemble active fire. Both the incident and the ordinary-day control were labelled "
     "active fire. At sites with flares, an image labelled fire therefore counts only as weak evidence."),
    ("fails", ["E016"], "Kstovo refinery: a city-level yes for the wrong reason",
     "The city check found 10 satellite passes with heat within 10 km, against about 3 expected, and returned corroborated. "
     "However, the heat was located 2–4 km from the refinery and was already present the day before. The site itself was under cloud. "
     "A positive city-level result does not establish a site-level one."),
    ("edge", ["E021"], "Engels oil depot: confirmed only by the aftermath",
     "FIRMS recorded nothing, as the depot was under cloud. The first clear image was taken 14 days later. "
     "It was labelled new damage or burn scar, which counts as corroborated. The result should be treated with caution. "
     "No clear image exists from before the strike, and this image had been seen before labelling."),
    ("edge", ["E019"], "Proletarsk oil depot: a city-level yes with a confounder",
     "The sources give no usable site location, so only the city check applies. It found 18 satellite passes with heat "
     "within 10 km, against 1.8 expected. The baseline covers the four preceding weeks of the same harvest season, "
     "so stubble burning is already included. The depot fire is the likely cause. The city check, however, cannot separate the two."),
]
CASE_GROUPS = {"works": "Where it works", "fails": "Where it fails or misleads", "edge": "Edge cases"}


def cases_html(D, have):
    out = []
    for g, gl in CASE_GROUPS.items():
        cards = []
        for grp, ids, title, text in CASES:
            if grp != g:
                continue
            img_id = next((i for i in ids if have[i]["blind"]), None)
            img = (f'<a href="events/{img_id}.html" class="case-img"><img src="img/thumb/{img_id}.jpg" alt="Sentinel-2 images before and after, {esc(D["ver"][img_id]["place_name"])}" loading="lazy" width="{THUMB_W}" height="{round(THUMB_W * 946 / 1430)}"></a>' if img_id else "")
            evs = " ".join(f'<a href="events/{i}.html">{i}</a> {pill(D["ver"][i]["site_verdict"])}' for i in ids)
            cards.append(f'<article class="case">{img}<div><h4>{esc(title)}</h4><p>{esc(text)}</p><p class="case-ev">{evs}</p></div></article>')
        out.append(f'<h3>{esc(gl)}</h3><div class="cases">{"".join(cards)}</div>')
    return "".join(out)


def cover(D, have):
    e = D["ev"][COVER_EVENT]
    s = D["s2"].get(COVER_EVENT, {})
    fig = ""
    if have.get(COVER_EVENT, {}).get("blind") and (SITE / "img/cover/sw_after.jpg").exists():
        before, after = fmt_date(s.get("before_date")), fmt_date(s.get("first_after_date"))
        tile = lambda n, lab: f'<div class="tile"><img src="img/cover/{n}.jpg" alt="" width="444" height="383"><span>{lab}</span></div>'
        fig = f"""<figure class="cover-fig">
      <div class="tiles">{tile('tc_before', 'Before')}{tile('tc_after', 'After')}{tile('sw_before', 'Before, infrared')}{tile('sw_after', 'After, infrared')}</div>
      <figcaption>{esc(e['place_name'].split(' (')[0])}, Russia. Sentinel-2, {esc(before)} and {esc(after)}. In infrared, fire shows orange. Contains modified Copernicus Sentinel data 2024.</figcaption>
    </figure>"""
    return f"""<section class="cover" aria-labelledby="title">
  <div class="wrap cover-inner">
    <div class="cover-text">
      <p class="kicker">Method test · {esc(datetime.now().strftime('%B %Y'))} · {esc(AUTHOR)}</p>
      <h1 id="title">{esc(TITLE)}</h1>
      <a class="enter" href="#summary">Continue to the results <span aria-hidden="true">↓</span></a>
      <p class="cover-sub">{esc(SUBTITLE)}</p>
    </div>
    {fig}
  </div>
</section>
"""


def index_page(D, N, have, built):
    a, t, x = N["all"], N["test"], N["cross"]
    lo, hi = wilson(a["ctl"]["k"], a["ctl"]["obs"])   # false alarms are only testable on observable controls
    ilo, ihi = wilson(a["inc"]["k"], a["inc"]["obs"])
    rules, thr = D["rules"], D["thr"]
    tw = rules["timing_window_hours"]
    n_img = len(D["blind"])
    seen = [e for e, b in D["blind"].items() if b.get("seen_before") == "True"]
    wiki = sum(1 for e in D["ev"].values() if e["lat_site"] and "Wikipedia" in e["site_coord_basis"])
    with_site = sum(1 for e in D["ev"].values() if e["lat_site"])
    n_sites = len({e["site_id"] for e in D["ev"].values() if e["lat_site"]})
    n_out = len(D["foot"]["features"])
    small = a["cats"]["small_or_indoor"]
    calib_ev = thr["evidence"]["calibration_result"]
    n_all = N["n_inc"] + N["n_ctl"]
    ctl_cloud = a["ctl"]["n"] - a["ctl"]["obs"]
    desc = (f"Can OpenStreetMap, NASA FIRMS and Sentinel-2 confirm reported fires, explosions and strikes? "
            f"Tested on {n_all} past events: {a['inc']['k']} of {a['inc']['obs']} observable incidents confirmed at the site, "
            f"no false alarm on {a['ctl']['obs']} observable controls.")
    return head(f"{TITLE}: OpenStreetMap, FIRMS, Sentinel-2", desc) + f"""</head>
<body>
<a class="skip" href="#summary">Skip to the summary</a>
{masthead()}
{cover(D, have)}
<main>
  <section class="wrap summary" id="summary" aria-labelledby="summary-h">
    <h2 id="summary-h">Summary</h2>
    <div class="intro">
      <p>This study tests whether open data can confirm reported fires, explosions and strikes. The test set contains {n_all} past events: {N['n_inc']} incidents and {N['n_ctl']} controls. A control is the same site on an ordinary day.</p>
      <p>Three sources are combined. OpenStreetMap provides the site outline. NASA FIRMS provides satellite heat detections. Sentinel-2 provides images before and after each event.</p>
    </div>
    <div class="stats">
      {stat(f"{a['inc']['k']}<span> of {a['inc']['obs']}</span>", "incidents confirmed at the site", f"Incidents with a clear view only. Test half: {t['inc']['k']} of {t['inc']['obs']}. 95% CI {pct(ilo)}–{pct(ihi)}.")}
      {stat(f"0<span> of {a['ctl']['obs']}</span>", "false alarms on controls", f"A further {ctl_cloud} controls were under cloud and could not be tested. Upper bound: {pct(hi)}.")}
      {stat(f"{x['site_only'] + x['both']}<span> vs {x['city_on_site_events']}</span>", "confirmed with the site outline vs with the city point", f"Same {x['n']} incidents.")}
      {stat(f"{x['nosite']}<span> of {N['n_inc']}</span>", "incidents with no usable site location", f"Only the city check applies. It confirmed {x['nosite_city']}.")}
    </div>
    <p class="answer"><b>Key finding.</b> Large fires at a known site were frequently confirmed. Small or indoor fires were not ({small['k']} of {small['obs']}). With only a city name, far fewer events were confirmed. Cloud prevented observation of {a['inc']['c']['not_observable']} incidents. Every verdict still requires human review.</p>
  </section>

  <section class="wrap" id="results" aria-labelledby="results-h">
    <h2 id="results-h">Results</h2>
    <p class="section-intro">Each event gets two verdicts. The site verdict uses the facility outline, a heat filter and the images. The city verdict uses only the city point a geocoder would return. Counts come with 95% Wilson intervals. Sample sizes are small, so the intervals are wide.</p>
    <div class="tabs" role="tablist" aria-label="Which events" hidden>
      <button type="button" role="tab" aria-selected="true" data-split="all">All {n_all}</button>
      <button type="button" role="tab" aria-selected="false" data-split="test">Test half</button>
      <button type="button" role="tab" aria-selected="false" data-split="calibration">Calibration half</button>
    </div>
    <div data-split-panel="all">{matrix(N, 'all')}</div>
    <div data-split-panel="test" hidden><p class="note">Test half: sites not used to tune any threshold. The verdict rules were written after some test cases had been seen (see <a href="#method">method</a>), so these results are semi-blind. Only {t['ctl']['obs']} of the {t['ctl']['n']} test controls had a clear view.</p>{matrix(N, 'test')}</div>
    <div data-split-panel="calibration" hidden><p class="note">Calibration half: the sites used to tune the heat filter.</p>{matrix(N, 'calibration')}</div>
    <dl class="legend-verdicts">
      <dt>{pill('corroborated')}</dt><dd>New heat at the site inside the time window, or a blind image label of fire, smoke or damage at a site without flares.</dd>
      <dt>{pill('ambiguous_attribution')}</dt><dd>The heat could belong to another known incident at the site.</dd>
      <dt>{pill('weak')}</dt><dd>An image labelled fire at a site with flares, or heat only after the window.</dd>
      <dt>{pill('not_observable')}</dt><dd>Nothing found, and the site was under cloud. No clear image within 5 days.</dd>
      <dt>{pill('not_corroborated')}</dt><dd>Nothing found, although the data were available.</dd>
      <dt>{pill('not_assessable_no_site')}</dt><dd>No usable site location in the sources. Only the city check applies.</dd>
    </dl>

    <h3>Site check vs city check</h3>
    <div class="cross">
      <table class="crosstab"><caption class="sr">Incidents with a site point: site-level vs city-level result</caption>
        <thead><tr><td></td><th scope="col">City: yes</th><th scope="col">City: no</th></tr></thead>
        <tbody>
          <tr><th scope="row">Site: corroborated</th><td>{x['both']}</td><td class="hl">{x['site_only']}</td></tr>
          <tr><th scope="row">Site: anything else</th><td class="hl2">{x['city_only']}</td><td>{x['neither']}</td></tr>
        </tbody>
      </table>
      <div>
        <p>Across the same {x['n']} incidents with a known site, the site check confirmed {x['site_only']} that the city check missed.</p>
        <p>The city check said yes once where the site check did not (<a href="events/E016.html">Kstovo</a>). That yes came from other heat in town.</p>
        <p>Neither check flagged a control. However, only {a['ctl']['obs']} of the {a['ctl']['n']} controls had a clear view.</p>
      </div>
    </div>

    <h3>Every event</h3>
    <p class="section-intro">All {n_all} events, including misses. Each row opens an evidence page with the location, FIRMS detections, satellite images and sources. "Map" shows the event on the map below.</p>
    {event_table(D)}
  </section>

  <section class="wrap" id="map-section" aria-labelledby="map-h">
    <h2 id="map-h">Map</h2>
    <p class="section-intro">Site colour shows the best verdict among the site's incidents. Hollow rings mark incidents without a site point. They sit on the city named in the news, inside the {rules['city_radius_km']:g} km search radius. Selecting a site shows its events, the FIRMS detections and one year of heat history.</p>
    <div class="map-layout">
      <div class="map-frame">
        <div id="map" role="region" aria-label="Map of event sites and city points"><noscript><p class="map-fallback">The map needs JavaScript. Every event is in the table above, with its own page.</p></noscript></div>
        <details class="map-panel" id="map-panel-wrap" hidden><summary>Layers</summary>
        <form id="map-panel" aria-label="Map layers">
          <fieldset><legend>Base map</legend>
            <label><input type="radio" name="base" value="dark" checked> VersaTiles (dark)</label>
            <label><input type="radio" name="base" value="osm"> OpenStreetMap</label>
            <label><input type="radio" name="base" value="satellite"> Satellite (Esri)</label>
          </fieldset>
          <fieldset><legend>Layers</legend>
            <label><input type="checkbox" name="layer" value="sites" checked><i class="sw sw-site"></i>Sites <span class="n">{n_sites}</span></label>
            <label><input type="checkbox" name="layer" value="nosite" checked><i class="sw sw-nosite"></i>No site point <span class="n">{x['nosite']}</span></label>
            <label><input type="checkbox" name="layer" value="city"><i class="sw sw-city"></i>City points, offset to site <span class="n">{n_all}</span></label>
            <label><input type="checkbox" name="layer" value="outlines" checked><i class="sw sw-outline"></i>OSM site outlines <span class="n">{n_out}</span></label>
          </fieldset>
        </form></details>
      </div>
      <aside class="detail" id="detail" aria-live="polite">
        <p class="muted">Select a site or a ring on the map, or use the Map button in the table.</p>
        <div class="map-legend">
          <p><b>Site colour</b></p>
          <p>{pill('corroborated')} {pill('weak')} {pill('not_corroborated')} {pill('not_observable')}</p>
          <p><b>FIRMS detections</b> (selected event)</p>
          <p><span class="key"><i class="sw sw-new"></i>new heat</span> <span class="key"><i class="sw sw-anom"></i>anomalous</span> <span class="key"><i class="sw sw-bg"></i>usual heat</span></p>
          <p><b>Heat history</b> (selected site)</p>
          <p><span class="key"><i class="sw sw-cell-p"></i>400 m cell, recurring heat</span> <span class="key"><i class="sw sw-cell"></i>occasional heat</span></p>
        </div>
      </aside>
    </div>
  </section>

  <section class="wrap" id="method" aria-labelledby="method-h">
    <h2 id="method-h">Method</h2>
    <p class="section-intro">Six stages. Each reads the previous stage's files and writes its own. Downloads are cached, so reruns give the same result. Tags show which steps run automatically and which require manual review.</p>
    <ol class="pipeline">
      <li><h4>01 Events <span class="tag human">manual</span></h4><p>{n_all} past events: four incident types plus {N['n_ctl']} controls. Each has a city point, the kind a geocoder returns. Where the sources allow, it also has a precise site point. Sites are split into a calibration half and a test half.</p></li>
      <li><h4>02 OpenStreetMap <span class="tag auto">automatic</span> <span class="tag human">manual review</span></h4><p>The script searches OSM by name near each site and proposes outlines. The correct one is selected by hand. {n_out} outlines were accepted. In three cases, the name search returned a neighbouring facility (Moss Landing, Crocus City Hall, Deer Park). For this reason the final choice is not left to the script.</p></li>
      <li><h4>04 FIRMS <span class="tag auto">automatic</span></h4><p>All VIIRS and MODIS heat detections in a box around each event. Five days around the event, plus a 30-day baseline. Days without satellite data are logged as gaps, so "nothing seen" is never confused with "no data".</p></li>
      <li><h4>05 Persistent heat <span class="tag auto">automatic</span></h4><p>One clean year of heat history per site, on a {thr['values']['CELL_M']} m grid. Each detection is labelled new, anomalous or background. Only new and anomalous count as fire. Thresholds were tuned on calibration sites and frozen on {esc(fmt_date(thr['frozen_on']))}, before any test result was examined.</p></li>
      <li><h4>07 Sentinel-2 <span class="tag auto">automatic</span> <span class="tag human">blind labels</span></h4><p>Three images per site: clear before, first after, clear after. A burn index (dNBR) compares the site with a ring around it. {n_img} image sets were labelled blind, with names hidden and controls mixed in. {len(seen)} had been seen before labelling ({', '.join(seen)}); their pages note this.</p></li>
      <li><h4>06 Verdicts <span class="tag auto">automatic</span></h4><p>Fixed rules turn the evidence into one verdict. Time window: {-tw['hour'][0]} h before to {tw['hour'][1]} h after when the hour is known. {-tw['day'][0]} h before to {tw['day'][1]} h after when only the day is known. Heat that starts within {rules['attribution_days_after']} days after another known incident at the site does not count. The city check counts satellite passes with heat within {rules['city_radius_km']:g} km of the city point. It compares them with that area's own {rules['baseline_days']}-day baseline (Poisson, p &lt; {rules['city_p_value']}).</p></li>
    </ol>
    <h3>Decisions made before and after examining results</h3>
    <div class="prose">
      <p>The heat thresholds were frozen before any test result was examined. The verdict rules were not. They were written after stages 04–07 had been run and part of the output inspected.</p>
      <p>Two rules came from specific cases. The attribution rule came from Volgograd E002, in the test half. The flare caveat came from the Garyville control. One rule was changed after the first run.</p>
      <p>The test-half results should therefore be read as semi-blind. The full disclosure is in <a href="data/verdict_rules.json">verdict_rules.json</a>.</p>
    </div>
    <h3>FIRMS at site scale</h3>
    <div class="prose">
      <p>NASA advises against using FIRMS for local-scale decisions. This study does exactly that. A VIIRS pixel is 375 m wide. A detection only says that something hot was near the site.</p>
      <p>A detection therefore counts only if three conditions hold. It is new against the site's own history, it falls inside the time window, and no other known incident explains it better.</p>
      <p>The controls measure how often this still goes wrong. The image is a second, independent check.</p>
    </div>
  </section>

  <section class="wrap" id="cases" aria-labelledby="cases-h">
    <h2 id="cases-h">Cases</h2>
    <p class="section-intro">Two cases where the method works, three where it fails or would have misled, and two edge cases. The images are those shown during labelling: before, first after and clear after. True colour on top, shortwave infrared below.</p>
    {cases_html(D, have)}
  </section>

  <section class="wrap" id="limits" aria-labelledby="limits-h">
    <h2 id="limits-h">Limits</h2>
    <ul class="limits">
      <li><b>Small samples.</b> {a['inc']['obs']} observable incidents. {a['ctl']['obs']} observable controls, {t['ctl']['obs']} of them in the test half. Cloud removed {ctl_cloud} controls as tests. The false-alarm rate is only bounded below {pct(hi)}. More controls on clear days would tighten it.</li>
      <li><b>Semi-blind test.</b> The verdict rules were written after some test cases had been seen.</li>
      <li><b>The heat filter costs hits.</b> On calibration sites it removed every false hit on controls. It also removed true ones: {esc(calib_ev)}.</li>
      <li><b>Small and indoor events are out of reach.</b> {small['k']} of {small['obs']} confirmed. A 375 m heat pixel and an image every few days miss short or covered fires.</li>
      <li><b>Cloud is judged from one image.</b> "Not observable" rests on one image taken around 10:30 local time. It stands in for the whole window.</li>
      <li><b>Site locations.</b> {wiki} of {with_site} site points come from Wikipedia coordinates. Only {n_out} sites have a reviewed outline. A wrong site point gives a wrong verdict.</li>
      <li><b>Ground truth.</b> Two events are disputed. One (Moscow, E005) rests on a single secondary source. One (Toropets) is partly circular: its source cites FIRMS.</li>
      <li><b>Past events only.</b> Live news is less reliable. First reports often give the wrong place or time.</li>
    </ul>
    <h3>Next steps</h3>
    <ul class="limits">
      <li>Hourly cloud cover at the satellite overpass times, instead of the one-image proxy.</li>
      <li>More controls, picked on clear days.</li>
      <li>A fresh, fully blind run on new events, with the rules unchanged.</li>
    </ul>
  </section>

  <section class="wrap" id="data" aria-labelledby="data-h">
    <h2 id="data-h">Data, code and credits</h2>
    <p><code>build_site.py</code> builds this site from the pipeline outputs. The code for every stage is in the <a href="{esc(REPO_URL)}">repository</a>. Downloads:</p>
    <ul class="downloads">
      <li><a href="data/events.csv">events.csv</a>: all events with locations, verdicts, reasons and sources (CC BY 4.0)</li>
      <li><a href="data/fire_detections.csv">fire_detections.csv</a>: FIRMS detections in the event windows, with heat-filter labels (CC BY 4.0)</li>
      <li><a href="data/heat_cells.geojson">heat_cells.geojson</a>: the 400 m heat-history grid per site (CC BY 4.0)</li>
      <li><a href="data/site_outlines_odbl.geojson">site_outlines_odbl.geojson</a>: the {n_out} accepted OpenStreetMap outlines (ODbL 1.0)</li>
      <li><a href="data/verdict_rules.json">verdict_rules.json</a>, <a href="data/frozen_thresholds.json">frozen_thresholds.json</a>, <a href="data/verdict_report.txt">verdict_report.txt</a></li>
    </ul>
    <h3>Sources and credits</h3>
    <div class="prose">
      <p>Fire detections: NASA FIRMS. We acknowledge the use of data from NASA's Fire Information for Resource Management System (FIRMS) (<a href="https://www.earthdata.nasa.gov/firms">earthdata.nasa.gov/firms</a>), part of NASA's Earth Science Data and Information System (ESDIS). Products: VIIRS 375 m active fire from S-NPP (doi:10.5067/VIIRS/VNP14IMG.002), NOAA-20 (doi:10.5067/VIIRS/VJ114IMG.002) and NOAA-21 (doi:10.5067/VIIRS/VJ214IMG.002). MODIS Collection 6.1 active fire (Giglio, Schroeder, Hall and Justice, <i>MODIS C6/C6.1 Active Fire Product User's Guide</i>, 2021). FIRMS data are provided as is. See the <a href="https://www.earthdata.nasa.gov/data/projects/lance/disclaimer">LANCE disclaimer</a>.</p>
      <p>Imagery: contains modified Copernicus Sentinel data 2021–2026, accessed through Element 84 Earth Search (<a href="https://registry.opendata.aws/sentinel-2-l2a-cogs/">registry.opendata.aws/sentinel-2-l2a-cogs</a>). Satellite base map: Esri World Imagery, © Esri, Maxar, Earthstar Geographics and the GIS User Community.</p>
      <p>Site outlines and base map: © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors (ODbL). ESA WorldCover 2021 (CC BY 4.0). Tiles by VersaTiles. City coordinates: <a href="https://www.geonames.org/">GeoNames</a> (CC BY 4.0). Site coordinates: Wikipedia and OpenStreetMap. Each event page links its news and official sources.</p>
      <p>Licences: code MIT. Data CC BY 4.0, except the OpenStreetMap outlines (ODbL 1.0).</p>
    </div>
    <h3 id="ai-use">Use of AI</h3>
    <div class="prose">
      <p>This study was carried out by {esc(AUTHOR)} with the assistance of Claude Opus 5.5, a large language model made by Anthropic. Claude wrote parts of the code, including the pipeline stages and this website. It also helped compile the event list and its sources, ran parts of the analysis and drafted text.</p>
      <p>Every output was reviewed by the author and checked against its sources. The OpenStreetMap outlines were selected and every image was labelled by the author, without event names. The verdict rules, assessments and conclusions are the author's responsibility.</p>
      <p>Corrections are welcome as GitHub issues. Please include a source.</p>
    </div>
  </section>
</main>
{footer(0, built)}
<script src="https://unpkg.com/maplibre-gl@{MAPLIBRE}/dist/maplibre-gl.js" integrity="{MAPLIBRE_JS_SRI}" crossorigin="anonymous" defer></script>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@{MAPLIBRE}/dist/maplibre-gl.css" integrity="{MAPLIBRE_CSS_SRI}" crossorigin="anonymous">
<script src="assets/site.js" defer></script>
</body>
</html>
"""


# ------------------------------------------------------------------ event pages
def dl(items):
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in items if v not in (None, ""))
    return f'<dl class="facts">{rows}</dl>'


def event_page(eid, D, have, order, built):
    e, v = D["ev"][eid], D["ver"][eid]
    f, h, s, b = D["firms"].get(eid, {}), D["heat"].get(eid, {}), D["s2"].get(eid, {}), D["blind"].get(eid, {})
    i = order.index(eid)
    prev_id, next_id = order[i - 1] if i else None, order[i + 1] if i + 1 < len(order) else None
    same_site = [x for x in order if x != eid and D["ev"][x]["site_id"] == e["site_id"] and e["lat_site"]]
    rel = ", ".join(f'<a href="{x}.html">{x}</a> ({esc(CATS[D["ver"][x]["category"]].lower())}, {esc(fmt_date(D["ev"][x]["event_time_utc"]).split(",")[0])})' for x in same_site)
    is_ctl = v["category"] == "negative_control"
    title = f"{v['place_name']}, {fmt_date(e['event_time_utc']).split(',')[0]}"
    desc = f"{eid}: {CATS[v['category']]} at {v['place_name']}. Site-level verdict: {SV.get(v['site_verdict'])}. City-level: {CV.get(v['city_verdict'])}."

    outline = next((ft["properties"] for ft in D["foot"]["features"] if ft["properties"]["site_id"] == e["site_id"]), None)
    loc = dl([
        ("Site point", f"{num(e['lat_site']):.5f}, {num(e['lon_site']):.5f}" if e["lat_site"] else "None. The sources give no usable site location. Left blank on purpose."),
        ("Basis", esc(e["site_coord_basis"]) if e["lat_site"] else ""),
        ("Uncertainty", f"about {num(e['site_uncertainty_km']):.1f} km" if num(e["site_uncertainty_km"]) is not None and e["lat_site"] else ""),
        ("OSM outline", (f'<a href="{esc(outline.get("osm_url"))}">{esc(outline.get("osm"))}</a>' + (f' "{esc(outline.get("osm_name"))}"' if outline.get("osm_name") else "")) if outline else ("none accepted" if e["lat_site"] else "")),
        ("City point", f"{esc(e['coarse_place'])}: {num(e['lat_coarse']):.4f}, {num(e['lon_coarse']):.4f}" if e["lat_coarse"] else esc(e["coarse_place"])),
        ("City to site", f"{esc(e['coarse_offset_km'])} km" if e["coarse_offset_km"] and e["lat_site"] else ""),
    ])

    if e["lat_site"]:
        firms = dl([
            ("Coverage", esc(f.get("event_coverage", "")).replace(" ", "<br>")),
            ("Gaps", esc(f.get("event_gaps")) or "none"),
            ("At the site", f"{esc(f.get('n_site_event') or 0)} detections in the 5 days around the event. Usual rate: {esc(f.get('n_site_baseline_per_5d') or '0')} per 5 days."),
            ("After the heat filter", (f"{esc(h.get('n_site_signal'))} count as fire ({esc(h.get('n_new'))} new, {esc(h.get('n_anomalous'))} anomalous). {esc(h.get('n_background'))} usual heat." if h else "")),
            ("First signal", hours(h.get("signal_first_hours")) + " from the event" if h.get("signal_first_hours") else ""),
            ("Strongest signal", f"{num(h.get('signal_max_frp_mw')):.0f} MW fire radiative power" if num(h.get("signal_max_frp_mw")) else ""),
        ])
    else:
        firms = dl([("At the site", "Not assessed. No site point."), ("Near the city", f"{esc(f.get('n_coarse_event') or 0)} detections in the search box")])

    city = dl([
        ("Verdict", pill(v["city_verdict"], "city")),
        ("Passes with heat", f"{esc(v['city_overpasses_in_window'])} within {D['rules']['city_radius_km']:g} km of the city point, inside the time window"),
        ("Expected", f"{esc(v['city_expected_from_baseline'])}, from the area's own {D['rules']['baseline_days']}-day baseline (p = {esc(v['city_p_value'])})"),
    ])

    s2rows = [("Status", esc(S2_STATUS.get(s.get("status"), s.get("status"))))]
    if s.get("before_date"):
        s2rows.append(("Before", f"{esc(fmt_date(s['before_date']))}. Cloud over site {pct(num(s.get('before_cloud'), 0))}."))
    if s.get("first_after_date"):
        s2rows.append(("First after", f"{esc(fmt_date(s['first_after_date']))} (+{esc(s.get('first_after_days'))} d). Cloud over site {pct(num(s.get('first_after_cloud'), 0))}."))
    if s.get("clear_after_date"):
        s2rows.append(("Clear after", f"{esc(fmt_date(s['clear_after_date']))} (+{esc(s.get('clear_after_days'))} d)"))
    if num(s.get("dnbr_site_minus_ring")) is not None:
        s2rows.append(("Burn change (dNBR)", f"Site minus surroundings: {num(s['dnbr_site_minus_ring']):+.3f}. {pct(num(s.get('frac_site_dnbr_gt_0_27'), 0))} of the site above 0.27."))
    if b:
        s2rows.append(("Blind label", f"<b>{esc(LABELS.get(b['label'], b['label']))}</b>, {esc(b.get('confidence'))} confidence" +
                       (' <span class="flag">seen before labelling</span>' if b.get("seen_before") == "True" else "")))
    s2 = dl(s2rows)

    imgs = ""
    if have[eid]["blind"]:
        imgs += (f'<figure class="shot"><a href="../img/blind/{eid}.jpg"><img src="../img/blind/{eid}.jpg" alt="Sentinel-2 true colour and shortwave infrared, before and after, as shown when labelling" loading="lazy" width="1430" height="946"></a>'
                 f'<figcaption><b>Images as shown during labelling.</b> Name and date hidden. Cyan: the OSM outline, or a circle for the site point’s uncertainty (at least 300 m). Top: true colour. Bottom: shortwave infrared (B12-B11-B8A), where fire shows orange. Label: {esc(LABELS.get(b.get("label"), "—"))}. Contains modified Copernicus Sentinel data.</figcaption></figure>')
    if have[eid]["panel"]:
        imgs += (f'<figure class="shot"><a href="../img/panels/{eid}.jpg"><img src="../img/panels/{eid}.jpg" alt="The same images with FIRMS detections drawn on" loading="lazy" width="1430" height="946"></a>'
                 f'<figcaption><b>The same images with FIRMS detections.</b> Red circles: fire signal. Grey crosses: usual heat. Every detection from the event window is drawn on every image, the "before" image included. Contains modified Copernicus Sentinel data.</figcaption></figure>')

    src = links(e["sources"])
    pager = lambda: (f'<nav class="pager" aria-label="Events">'
                     + (f'<a href="{prev_id}.html">← {prev_id}</a>' if prev_id else "<span></span>")
                     + '<a href="../index.html#results">All events</a>'
                     + (f'<a href="{next_id}.html">{next_id} →</a>' if next_id else "<span></span>") + "</nav>")
    return head(f"{title} · {eid}", desc, depth=1) + f"""</head>
<body>
{masthead(1)}
<main class="wrap event">
  {pager()}
  <p class="kicker">{eid} · {esc(CATS[v['category']])} · {esc(SPLIT.get(v['split'], v['split']))}</p>
  <h1 class="event-title">{esc(v['place_name'])}</h1>
  <p class="event-meta">{esc(fmt_date(e['event_time_utc']))}{' (day known, not the hour)' if e['time_precision'] == 'day' else ''} · ground truth: <b>{esc(v['ground_truth'])}</b> · <a href="../index.html#map={eid}">show on map</a></p>
  {'<p class="control-note">Control: the same site on an ordinary day, with no known incident. Any corroboration here is a false alarm.</p>' if is_ctl else ''}
  {f'<p class="related">Other events at this site: {rel}</p>' if rel else ''}

  <div class="verdicts">
    <div class="verdict-box"><p class="vb-h">Site level</p><p>{pill(v['site_verdict'])}{f' <span class="muted">evidence: {esc(v["site_evidence"])}</span>' if v['site_evidence'] else ''}</p><p>{esc(v['site_reason'])}</p></div>
    <div class="verdict-box"><p class="vb-h">City level</p><p>{pill(v['city_verdict'], 'city')}</p><p>{esc(v['city_overpasses_in_window'])} satellite passes with heat within {D['rules']['city_radius_km']:g} km. Expected: {esc(v['city_expected_from_baseline'])}.</p></div>
  </div>

  <div class="event-grid">
    <section><h2>Location</h2>{loc}</section>
    <section><h2>FIRMS heat detections</h2>{firms}</section>
    <section><h2>City check</h2>{city}</section>
    <section><h2>Sentinel-2</h2>{s2}</section>
  </div>
  {f'<section class="images"><h2>Images</h2>{imgs}</section>' if imgs else ''}
  <section class="event-sources"><h2>Ground truth and sources</h2>
    <p>{esc(e['ground_truth_basis'])}</p>
    {'<p class="control-note">Partly circular. A ground-truth source itself relies on satellite fire data. A FIRMS match here is not fully independent.</p>' if e.get('circular_risk') == 'partial' else ''}
    {f'<p class="note">Note: {esc(e["notes"])}</p>' if e['notes'] else ''}
    <p class="sources">{' '.join(src) if src else 'No source link.'}</p>
  </section>
  {pager()}
</main>
{footer(1, built)}
</body>
</html>
"""


# ------------------------------------------------------------------ main
def main():
    D = load()
    N = compute(D)
    check_against_report(N, D["report"])
    for d in ("assets", "events", "data"):
        (SITE / d).mkdir(parents=True, exist_ok=True)
    have, made = make_images(D)
    write_data(D, have)
    shutil.copy(SRC / "site.css", SITE / "assets/site.css")
    shutil.copy(SRC / "site.js", SITE / "assets/site.js")
    for name in ("README.md", "LICENSE", "DATA_LICENSE.md"):
        if (SRC / name).exists():
            shutil.copy(SRC / name, SITE / name)
    (SITE / ".nojekyll").write_text("")
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (SITE / "index.html").write_text(no_widows(index_page(D, N, have, built)), encoding="utf-8")
    order = sorted(D["ver"])
    for eid in order:
        (SITE / "events" / f"{eid}.html").write_text(no_widows(event_page(eid, D, have, order, built)), encoding="utf-8")
    a = N["all"]
    print(f"site built in {SITE}: index + {len(order)} event pages, {made} images re-encoded")
    print(f"  site level {a['inc']['k']}/{a['inc']['obs']} (test {N['test']['inc']['k']}/{N['test']['inc']['obs']}), "
          f"controls {a['ctl']['k']}/{a['ctl']['n']}, city {a['inc']['city']}/{N['n_inc']}: matches verdict_report.txt")


if __name__ == "__main__":
    main()
