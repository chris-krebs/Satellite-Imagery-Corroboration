# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "shapely>=2", "pyproj"]
# ///
"""Separate fire signal from persistent industrial heat (flares, furnaces) at each site.

    uv run src/persistent_heat.py run --only S04 S09     # two sites first
    uv run src/persistent_heat.py run                    # every site with coordinates
    uv run src/persistent_heat.py run --offline          # rebuild outputs from the cache
    uv run src/persistent_heat.py sanity                 # known FIRMS-visible fires: does the filter keep them?

Runs with --only replace those sites in the outputs and keep the others.

1. History. For each site, one year of VIIRS detections that should contain no incidents:
   the 365 days ending 60 days before the earliest known incident there (events in the event
   list plus data/known_incidents.csv). Left out of that year:
     - every evaluation window at the site, controls included (event day -8 ... +10),
       so a control is never judged against a history that contains its own detections;
     - 30 days either side of every known incident (a whole month for month-only dates).
2. Persistence map. History detections on a 400 m grid around the site (FIRMS builds its own
   static-source mask the same way): days with detections, months active, FRP median / p95.
3. Labels. Every event-window detection within 5 km of the site, from 04_FIRMS, gets:
     new         no history anywhere within its pixel footprint             -> signal
     anomalous   history exists, but FRP > 2x that history's p95, or the
                 overpass lit up far more grid cells than any usual overpass -> signal
     background  history exists and the detection looks like it             -> not signal
   Nothing is deleted; the labels and the numbers behind them are all in the output.
4. Report. Controls and confirmed events, before (any detection at the site) and after
   (signal detections only), split into calibration and test sites.

Inputs:  ../02_OSM_CHECK/data/events_reviewed.csv, site_footprints.geojson,
         ../04_FIRMS/outputs/firms_detections.csv, data/known_incidents.csv, FIRMS key (../.env)
Outputs: outputs/heat_cells.csv + .geojson   persistence map per site (load in QGIS)
         outputs/labelled_detections.csv      event-window detections with labels
         outputs/heat_event_summary.csv       per event: raw vs signal counts
         outputs/heat_history.csv             history window, exclusions, coverage per site
"""
import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 05_PERSISTENT_HEAT
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT / "04_FIRMS" / "src"))
import firms                                        # reuse the FIRMS client, request planning, helpers

from pyproj import Transformer
from shapely.geometry import Point, box as shp_box, mapping, shape
from shapely.ops import transform

EVENTS = PROJECT / "02_OSM_CHECK" / "data" / "events_reviewed.csv"
FOOTPRINTS = PROJECT / "02_OSM_CHECK" / "data" / "site_footprints.geojson"
DETECTIONS = PROJECT / "04_FIRMS" / "outputs" / "firms_detections.csv"
INCIDENTS = ROOT / "data" / "known_incidents.csv"
CACHE = ROOT / "data" / "cache" / "firms"
OUT = ROOT / "outputs"

HISTORY_SENSORS = ["VIIRS_SNPP", "VIIRS_NOAA20"]   # 375 m; NOAA-21 only exists from 2023
HISTORY_DAYS = 365
HISTORY_GAP_DAYS = 60          # history ends this long before the earliest incident
EVAL_EXCLUDE = (-8, 10)        # around every evaluation window day at the site
INCIDENT_EXCLUDE_DAYS = 30
BOX_MARGIN_KM = 3.0            # history box = footprint (or point + uncertainty) + this
SKIP_UNCERTAINTY_KM = 5.0      # ship positions this vague are skipped (no fixed site)

CELL_M = 400.0                 # grid size, as in the FIRMS static thermal anomaly mask
LABEL_RADIUS_KM = 5.0          # label event detections this close to the site
PERSIST_MIN_DAYS, PERSIST_MIN_MONTHS = 5, 2   # for the map only: "persistent" cell
FRP_FACTOR = 2.0               # anomalous if FRP > this x history p95 nearby
CLUSTER_MIN, CLUSTER_FACTOR = 3, 2.0          # anomalous if overpass lights > max(3, 2x usual) cells


# ------------------------------------------------------------------ dates and windows
def parse_incident_date(v):
    """'2025-01-24' -> (d, d); '2024-02' -> (first, last day of month)."""
    v = v.strip()
    if len(v) == 7:
        y, m = int(v[:4]), int(v[5:7])
        first = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        return first, nxt - timedelta(days=1)
    d = date.fromisoformat(v)
    return d, d


def load_incidents(path):
    out = defaultdict(list)
    if path.exists():
        for r in csv.DictReader(open(path, encoding="utf-8")):
            if r.get("site_id") and r.get("date"):
                out[r["site_id"].strip()].append(parse_incident_date(r["date"]))
    return out


def history_plan(site_events, incidents):
    """(start, end, excluded set of days) for one site's clean history year."""
    starts = [firms.parse_event_time(e["event_time_utc"]).date() for e in site_events
              if e["category"] != "negative_control"]
    starts += [a for a, _ in incidents]
    earliest = min(starts)
    end = earliest - timedelta(days=HISTORY_GAP_DAYS)
    start = end - timedelta(days=HISTORY_DAYS - 1)
    excluded = set()
    for e in site_events:                                    # all evaluation windows, controls too
        d0 = firms.parse_event_time(e["event_time_utc"]).date()
        excluded |= set(firms.daterange(d0 + timedelta(days=EVAL_EXCLUDE[0]), d0 + timedelta(days=EVAL_EXCLUDE[1])))
    for a, b in incidents:
        excluded |= set(firms.daterange(a - timedelta(days=INCIDENT_EXCLUDE_DAYS), b + timedelta(days=INCIDENT_EXCLUDE_DAYS)))
    excluded = {d for d in excluded if start <= d <= end}
    return start, end, excluded


def included_segments(start, end, excluded):
    """Consecutive runs of included days."""
    segs, cur = [], None
    for d in firms.daterange(start, end):
        if d in excluded:
            cur = None
            continue
        if cur and cur[1] + timedelta(days=1) == d:
            cur[1] = d
        else:
            cur = [d, d]
            segs.append(cur)
    return [tuple(s) for s in segs]


# ------------------------------------------------------------------ geometry
class Grid:
    """400 m cells in a local projection centred on the site."""
    def __init__(self, lat, lon):
        crs = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m"
        self.fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
        self.inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform

    def xy(self, lat, lon):
        return self.fwd(lon, lat)

    def cell(self, lat, lon):
        x, y = self.xy(lat, lon)
        return math.floor(x / CELL_M), math.floor(y / CELL_M)

    def cells_within(self, lat, lon, radius_m):
        """Cells whose square comes within radius_m of the point."""
        x, y = self.xy(lat, lon)
        k = math.ceil(radius_m / CELL_M) + 1
        cx, cy = math.floor(x / CELL_M), math.floor(y / CELL_M)
        out = []
        for i in range(cx - k, cx + k + 1):
            for j in range(cy - k, cy + k + 1):
                dx = max(i * CELL_M - x, 0, x - (i + 1) * CELL_M)
                dy = max(j * CELL_M - y, 0, y - (j + 1) * CELL_M)
                if math.hypot(dx, dy) <= radius_m:
                    out.append((i, j))
        return out

    def cell_polygon(self, c):
        i, j = c
        return transform(self.inv, shp_box(i * CELL_M, j * CELL_M, (i + 1) * CELL_M, (j + 1) * CELL_M))


def history_box(site, footprint):
    lat, lon = float(site["lat_site"]), float(site["lon_site"])
    unc = firms.to_float(site.get("site_uncertainty_km"), 1.0) or 1.0
    b = firms.box_around(lat, lon, unc + BOX_MARGIN_KM)
    if footprint is not None:
        w, s, e, n = footprint.bounds
        fb = firms.box_around(s, w, BOX_MARGIN_KM)
        nb = firms.box_around(n, e, BOX_MARGIN_KM)
        b = firms.union_box(b, (fb[0], fb[1], nb[2], nb[3]))
    return b


def p95(values):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, math.ceil(0.95 * len(v)) - 1)]


# ------------------------------------------------------------------ main steps
def build_history(client, avail, site, footprint, site_events, incidents):
    start, end, excluded = history_plan(site_events, incidents)
    segs = included_segments(start, end, excluded)
    box = history_box(site, footprint)
    dets, gaps, failed, observed = [], 0, [], defaultdict(int)
    for sensor in HISTORY_SENSORS:
        for a, b in segs:
            runs, g = firms.plan_requests(sensor, a, b, avail)
            gaps += len(g)
            for product, s, n in runs:
                rows, status = client.area(product, box, s, n, f"{site['site_id']}:history")
                if rows is None:
                    failed.append(f"{product}@{s}:{status}")
                    continue
                observed[sensor] += n
                for r in rows:
                    t = r.get("acq_time", "").strip().zfill(4)
                    dets.append({"lat": float(r["latitude"]), "lon": float(r["longitude"]),
                                 "date": r["acq_date"].strip(), "frp": firms.to_float(r.get("frp"), 0.0),
                                 "sat": r.get("satellite", ""), "overpass": f"{r.get('satellite', '')}@{r['acq_date']}T{t}"})
    info = {"site_id": site["site_id"], "history_start": start.isoformat(), "history_end": end.isoformat(),
            "excluded_days": len(excluded), "included_days": HISTORY_DAYS - len(excluded),
            "observed_sensor_days": " ".join(f"{k}:{v}" for k, v in observed.items()),
            "gap_sensor_days": gaps, "failed_requests": len(failed), "n_history_detections": len(dets),
            "box": firms.fmt_box(box)}
    return dets, info, failed


def summarise_cells(grid, dets):
    cells = defaultdict(list)
    for d in dets:
        cells[grid.cell(d["lat"], d["lon"])].append(d)
    return cells


def neighbourhood_stats(cells, keys):
    ds = [d for k in keys for d in cells.get(k, [])]
    days = {d["date"] for d in ds}
    months = {d["date"][:7] for d in ds}
    frps = [d["frp"] for d in ds if d["frp"] is not None]
    return {"hist_days": len(days), "hist_months": len(months), "hist_n": len(ds),
            "hist_frp_p95": round(p95(frps), 2) if frps else ""}


def overpass_cell_counts(grid, dets, site_lat, site_lon):
    """Per overpass: number of distinct grid cells lit within LABEL_RADIUS of the site."""
    by = defaultdict(set)
    for d in dets:
        if firms.haversine_km(d["lat"], d["lon"], site_lat, site_lon) <= LABEL_RADIUS_KM:
            by[d["overpass"]].add(grid.cell(d["lat"], d["lon"]))
    return {k: len(v) for k, v in by.items()}


def label_detections(site, grid, cells, hist_overpass_counts, det_rows):
    """Label event-window detections near one site. Returns labelled rows."""
    usual = p95(list(hist_overpass_counts.values())) or 0
    cluster_bar = max(CLUSTER_MIN, CLUSTER_FACTOR * usual)
    near = [r for r in det_rows if r["dist_site_km"] != "" and float(r["dist_site_km"]) <= LABEL_RADIUS_KM]
    # cells lit per overpass in the event window
    lit = defaultdict(set)
    for r in near:
        t = r["acq_utc"]
        lit[f"{r['satellite']}@{t}"].add(grid.cell(float(r["latitude"]), float(r["longitude"])))
    out = []
    for r in near:
        lat, lon = float(r["latitude"]), float(r["longitude"])
        pix = firms.to_float(r.get("pixel_half_km"), 0.3) or 0.3
        keys = grid.cells_within(lat, lon, max(600.0, pix * 1000))
        st = neighbourhood_stats(cells, keys)
        frp = firms.to_float(r.get("frp_mw"), 0.0)
        n_lit = len(lit[f"{r['satellite']}@{r['acq_utc']}"])
        reasons = []
        if st["hist_n"] == 0:
            label = "new"
        else:
            if st["hist_frp_p95"] != "" and frp > FRP_FACTOR * st["hist_frp_p95"]:
                reasons.append(f"frp {frp:g} > {FRP_FACTOR:g}x p95 {st['hist_frp_p95']}")
            if n_lit > cluster_bar:
                reasons.append(f"{n_lit} cells lit > usual {cluster_bar:g}")
            label = "anomalous" if reasons else "background"
        out.append({**r, **st, "overpass_cells_lit": n_lit, "usual_cells_lit_p95": usual,
                    "label": label, "signal": label in ("new", "anomalous"), "reason": "; ".join(reasons)})
    return out


def split_of(site_id):
    return "calibration" if int(hashlib.md5(site_id.encode()).hexdigest(), 16) % 2 == 0 else "test"


def cmd_run(args):
    key = firms.load_key()
    if not key and not args.offline:
        sys.exit("no FIRMS map key: set FIRMS_MAP_KEY or put it in " + str(PROJECT / ".env"))
    firms.CACHE = CACHE                                   # history requests get their own cache
    client = firms.Firms(key, offline=args.offline)
    client.key_status()
    avail = client.availability()
    footprints = firms.load_footprints(Path(args.footprints))
    incidents = load_incidents(Path(args.incidents))

    events = list(csv.DictReader(open(args.events, encoding="utf-8")))
    by_site = defaultdict(list)
    for e in events:
        if e["lat_site"]:
            by_site[e["site_id"]].append(e)
    dets_04 = list(csv.DictReader(open(args.detections, encoding="utf-8")))
    ev_dets = defaultdict(list)
    for r in dets_04:
        if r["window"] == "event":
            ev_dets[r["event_id"]].append(r)

    cell_rows, cell_feats, labelled, summaries, hist_rows, done = [], [], [], [], [], set()
    for sid in sorted(by_site):
        if args.only and sid not in args.only:
            continue
        site_events = by_site[sid]
        site = site_events[0]
        unc = firms.to_float(site.get("site_uncertainty_km"), 0) or 0
        if unc > SKIP_UNCERTAINTY_KM:
            print(f"{sid} skipped: position uncertainty {unc:g} km (ship at sea)")
            continue
        fp = footprints.get(sid)
        print(f"{sid} {site['place_name'][:55]}", flush=True)
        ctx = process_site(client, avail, site, fp, site_events, incidents.get(sid, []))
        done.add(sid)
        hist_rows.append({**ctx["info"], "split": split_of(sid)})
        cell_rows += ctx["cell_rows"]
        cell_feats += ctx["cell_feats"]
        for e in site_events:
            lab = label_detections(site, ctx["grid"], ctx["cells"], ctx["hist_counts"], ev_dets.get(e["event_id"], []))
            labelled.extend(lab)
            summaries.append({
                "event_id": e["event_id"], "site_id": sid, "split": split_of(sid), "category": e["category"],
                "ground_truth": e["ground_truth"], "place_name": e["place_name"], "event_time_utc": e["event_time_utc"],
                **site_counts(lab),
                "history_detections": ctx["info"]["n_history_detections"],
                "history_failed_requests": ctx["info"]["failed_requests"],
            })

    # Merge into earlier outputs: a run with --only replaces those sites and keeps the others.
    OUT.mkdir(exist_ok=True)
    merge_csv(OUT / "heat_cells.csv", cell_rows, done)
    merge_geojson(OUT / "heat_cells.geojson", cell_feats, done)
    merge_csv(OUT / "labelled_detections.csv", labelled, done)
    all_summaries = merge_csv(OUT / "heat_event_summary.csv", summaries, done)
    merge_csv(OUT / "heat_history.csv", hist_rows, done)
    report(all_summaries)


def process_site(client, avail, site, fp, site_events, incidents):
    """History, persistence map and per-overpass cell counts for one site."""
    dets, info, failed = build_history(client, avail, site, fp, site_events, incidents)
    print(f"    history {info['history_start']} .. {info['history_end']}, {info['excluded_days']} days excluded, "
          f"{info['n_history_detections']} detections" + (f", {len(failed)} FAILED" if failed else ""), flush=True)
    sid = site["site_id"]
    slat, slon = float(site["lat_site"]), float(site["lon_site"])
    grid = Grid(slat, slon)
    cells = summarise_cells(grid, dets)
    cell_rows, cell_feats = [], []
    for c, ds in cells.items():
        days = {d["date"] for d in ds}
        months = {d["date"][:7] for d in ds}
        frps = [d["frp"] for d in ds]
        poly = grid.cell_polygon(c)
        row = {"site_id": sid, "cell": f"{c[0]},{c[1]}", "lat": round(poly.centroid.y, 5), "lon": round(poly.centroid.x, 5),
               "n_det": len(ds), "n_days": len(days), "n_months": len(months),
               "frp_median": round(statistics.median(frps), 2), "frp_p95": round(p95(frps), 2),
               "persistent": len(days) >= PERSIST_MIN_DAYS and len(months) >= PERSIST_MIN_MONTHS,
               "in_footprint": bool(fp is not None and fp.intersects(poly))}
        cell_rows.append(row)
        cell_feats.append({"type": "Feature", "geometry": mapping(poly), "properties": row})
    return {"grid": grid, "cells": cells, "hist_counts": overpass_cell_counts(grid, dets, slat, slon),
            "info": info, "cell_rows": cell_rows, "cell_feats": cell_feats}


def site_counts(lab):
    at_site = [r for r in lab if str(r["site_match"]) == "True"]
    sig = [r for r in at_site if r["signal"]]
    return {
        "n_site_raw": len(at_site), "n_site_signal": len(sig),
        "n_new": sum(r["label"] == "new" for r in at_site),
        "n_anomalous": sum(r["label"] == "anomalous" for r in at_site),
        "n_background": sum(r["label"] == "background" for r in at_site),
        "raw_first_hours": min((float(r["hours_from_event"]) for r in at_site), default=""),
        "signal_first_hours": min((float(r["hours_from_event"]) for r in sig), default=""),
        "raw_max_frp_mw": max((firms.to_float(r["frp_mw"], 0) for r in at_site), default=""),
        "signal_max_frp_mw": max((firms.to_float(r["frp_mw"], 0) for r in sig), default=""),
    }


def merge_csv(path, new_rows, replaced_sites):
    """Keep earlier rows for sites not in replaced_sites, add new rows; return all rows."""
    old = []
    if path.exists() and path.stat().st_size:
        old = [r for r in csv.DictReader(open(path, encoding="utf-8")) if r.get("site_id") not in replaced_sites]
    rows = old + new_rows
    if rows:
        cols = list(dict.fromkeys(k for r in rows for k in r.keys()))
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, restval="", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    return rows


def merge_geojson(path, new_feats, replaced_sites):
    old = []
    if path.exists() and path.stat().st_size:
        old = [f for f in json.loads(path.read_text(encoding="utf-8")).get("features", [])
               if f["properties"].get("site_id") not in replaced_sites]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": old + new_feats}), encoding="utf-8")


def report(summaries):
    print("\n                         before filter      after filter")
    for split in ("calibration", "test"):
        rows = [s for s in summaries if s["split"] == split]
        ctrl = [s for s in rows if s["category"] == "negative_control"]
        pos = [s for s in rows if s["category"] != "negative_control" and s["ground_truth"] == "confirmed"]
        hit = lambda s, k: int(s[k] or 0) > 0          # rows may come back from CSV as strings
        fa_raw = sum(hit(s, "n_site_raw") for s in ctrl); fa_sig = sum(hit(s, "n_site_signal") for s in ctrl)
        tp_raw = sum(hit(s, "n_site_raw") for s in pos); tp_sig = sum(hit(s, "n_site_signal") for s in pos)
        print(f"{split:12} controls with a hit   {fa_raw:3} / {len(ctrl):<3}         {fa_sig:3} / {len(ctrl)}")
        print(f"{'':12} events with a hit     {tp_raw:3} / {len(pos):<3}         {tp_sig:3} / {len(pos)}")
    print("Tune thresholds on 'calibration' only; quote 'test'.")


def fetch_window_detections(client, avail, ev, fp):
    """Event-window detections for one case, measured the same way as 04_FIRMS."""
    ev = dict(ev)
    ev["_t0"] = firms.parse_event_time(ev["event_time_utc"])
    box = firms.event_box(ev)
    fwd = firms.local_projection(float(ev["lat_site"]), float(ev["lon_site"]))
    d0 = ev["_t0"].date()
    first, last = d0 + timedelta(days=firms.EVENT_WINDOW[0]), d0 + timedelta(days=firms.EVENT_WINDOW[1])
    rows, seen, failed = [], set(), []
    for sensor in firms.DEFAULT_SENSORS:
        runs, _ = firms.plan_requests(sensor, first, last, avail)
        for product, s, n in runs:
            got, status = client.area(product, box, s, n, f"{ev['event_id']}:sanity")
            if got is None:
                failed.append(f"{product}@{s}:{status}")
                continue
            for det in got:
                k = (det.get("latitude"), det.get("longitude"), det.get("acq_date"), det.get("acq_time"), det.get("satellite"))
                if k in seen:
                    continue
                seen.add(k)
                m = firms.measure(ev, det, fp, fwd)
                rows.append({"event_id": ev["event_id"], "site_id": ev["site_id"], "window": "event", "product": product,
                             "satellite": det.get("satellite", ""), "latitude": det["latitude"], "longitude": det["longitude"],
                             **{k2: (str(v) if isinstance(v, bool) else v) for k2, v in m.items()},
                             "frp_mw": det.get("frp", "")})
    return rows, failed


def cmd_sanity(args):
    """Known FIRMS-visible refinery fires: does the filter keep them? Never part of the evaluation."""
    key = firms.load_key()
    if not key and not args.offline:
        sys.exit("no FIRMS map key: set FIRMS_MAP_KEY or put it in " + str(PROJECT / ".env"))
    firms.CACHE = CACHE
    client = firms.Firms(key, offline=args.offline)
    client.key_status()
    avail = client.availability()
    footprints = firms.load_footprints(Path(args.footprints))
    incidents = load_incidents(Path(args.incidents))
    by_site = defaultdict(list)
    for e in csv.DictReader(open(args.events, encoding="utf-8")):
        if e["lat_site"]:
            by_site[e["site_id"]].append(e)
    cases = list(csv.DictReader(open(args.cases, encoding="utf-8")))

    out, labelled = [], []
    for c in cases:
        sid = c["site_id"]
        if sid not in by_site:
            print(f"{c['case_id']}: site {sid} has no coordinates - skipped")
            continue
        site_events, site, fp = by_site[sid], by_site[sid][0], footprints.get(sid)
        inc = list(incidents.get(sid, []))
        day = parse_incident_date(c["date"])
        if day not in inc:
            inc.append(day)                  # the sanity fire itself must never sit in the history
        print(f"{c['case_id']} {sid} {site['place_name'][:45]} {c['date']}", flush=True)
        ctx = process_site(client, avail, site, fp, site_events, inc)
        ev = {**site, "event_id": c["case_id"], "event_time_utc": c["date"], "time_precision": c.get("time_precision", "day")}
        dets, failed = fetch_window_detections(client, avail, ev, fp)
        lab = label_detections(site, ctx["grid"], ctx["cells"], ctx["hist_counts"], dets)
        labelled.extend(lab)
        n = site_counts(lab)
        outcome = ("NO RAW DETECTIONS - case cannot test the filter" if n["n_site_raw"] == 0 else
                   "kept" if n["n_site_signal"] > 0 else "DROPPED - thresholds too strict for this case")
        out.append({"case_id": c["case_id"], "site_id": sid, "place_name": site["place_name"], "date": c["date"],
                    **n, "outcome": outcome, "failed_requests": len(failed),
                    "history": f"{ctx['info']['history_start']}..{ctx['info']['history_end']}",
                    "source": c.get("source", ""), "note": c.get("note", "")})
        print(f"    raw {n['n_site_raw']} (max {n['raw_max_frp_mw']} MW) -> signal {n['n_site_signal']} "
              f"(new {n['n_new']}, anomalous {n['n_anomalous']}, background {n['n_background']}): {outcome}", flush=True)
    OUT.mkdir(exist_ok=True)
    firms.write_csv(OUT / "sanity_summary.csv", out)
    firms.write_csv(OUT / "sanity_labelled.csv", labelled)
    print(f"\nsanity cases written to {OUT / 'sanity_summary.csv'} (kept out of heat_event_summary.csv)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sanity", help="check the filter keeps known FIRMS-visible fires (not part of the evaluation)")
    s.add_argument("--events", default=EVENTS)
    s.add_argument("--footprints", default=FOOTPRINTS)
    s.add_argument("--incidents", default=INCIDENTS)
    s.add_argument("--cases", default=ROOT / "data" / "sanity_cases.csv")
    s.add_argument("--offline", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--events", default=EVENTS)
    r.add_argument("--footprints", default=FOOTPRINTS)
    r.add_argument("--detections", default=DETECTIONS)
    r.add_argument("--incidents", default=INCIDENTS)
    r.add_argument("--only", nargs="*", help="site_ids, e.g. S04 S09")
    r.add_argument("--offline", action="store_true")
    args = p.parse_args()
    {"run": cmd_run, "sanity": cmd_sanity}[args.cmd](args)


if __name__ == "__main__":
    main()
