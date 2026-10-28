# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "shapely>=2", "pyproj"]
# ///
"""Fetch NASA FIRMS thermal detections around each event and measure them. No verdicts yet.

    uv run src/firms.py run --only E021 E019     # try a couple of events first
    uv run src/firms.py run                      # all events
    uv run src/firms.py run --offline            # rebuild outputs from the cache only

For every event (controls included) it downloads detections in a box around the site point
and the coarse point, for two time windows:

  event window     event day - 1 ... event day + 3            (5 days)
  baseline window  event day - 37 ... event day - 8           (30 days, for persistent heat)

and for each satellite it picks, day by day, the archived "SP" product where it exists and the
near-real-time "NRT" product otherwise. Days with neither are recorded as coverage gaps, so
"no detection" is never confused with "no data".

Inputs (from the OSM stage)
  ../02_OSM_CHECK/data/events_reviewed.csv     events with reviewed site coordinates
  ../02_OSM_CHECK/data/site_footprints.geojson outlines of accepted sites
  FIRMS map key in the environment variable FIRMS_MAP_KEY, or in ../.env as FIRMS_MAP_KEY=...

Outputs
  outputs/firms_detections.csv     every detection in each event's box, with distances and times
  outputs/firms_event_summary.csv  one row per event: coverage, counts near site / coarse point
  outputs/firms_requests.csv       every request made or read from cache
  data/cache/firms/                raw CSV per request (never contains the key)
"""
import argparse
import csv
import io
import json
import math
import os
import random
import sys
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parents[1]          # 04_FIRMS
PROJECT = ROOT.parent                               # XX_SATELLITE_COROBORATION
EVENTS = PROJECT / "02_OSM_CHECK" / "data" / "events_reviewed.csv"
FOOTPRINTS = PROJECT / "02_OSM_CHECK" / "data" / "site_footprints.geojson"
CACHE = ROOT / "data" / "cache" / "firms"
OUT = ROOT / "outputs"

API = "https://firms.modaps.eosdis.nasa.gov"
USER_AGENT = os.environ.get("FIRMS_UA", "sv-corroboration-firms/0.1")

# Satellite -> (archived product, near-real-time product). VIIRS_NOAA21_SP is not in the API docs
# but is listed by /api/data_availability; plan_requests only uses products that list includes.
SENSORS = {
    "VIIRS_SNPP": ("VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"),
    "VIIRS_NOAA20": ("VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT"),
    "VIIRS_NOAA21": ("VIIRS_NOAA21_SP", "VIIRS_NOAA21_NRT"),
    "MODIS": ("MODIS_SP", "MODIS_NRT"),
}
DEFAULT_SENSORS = ["VIIRS_SNPP", "VIIRS_NOAA20", "VIIRS_NOAA21", "MODIS"]

EVENT_WINDOW = (-1, 3)          # days relative to the event day, inclusive
BASELINE_WINDOW = (-37, -8)     # 30 days, ending a week before the event
MAX_DAY_RANGE = 5               # the API rejects more ("Invalid day range. Expects [1..5]"),
                                # although its documentation page says 1..10

SITE_BOX_MIN_KM = 10.0          # box half-size around the site point (grows with uncertainty)
COARSE_BOX_KM = 15.0            # box half-size around the coarse point

# Measurement radii. These are starting points to look at the data with, not tuned thresholds.
SITE_MATCH_BUFFER_KM = 0.5      # outline (or point + uncertainty) + pixel half-size + this
COARSE_MATCH_KM = 10.0          # "within 10 km of the city centroid", what a naive pipeline would use

# FIRMS: 5000 transactions per 10 minutes per key. Multi-day requests count as more than one;
# the docs do not say exactly how, so assume one transaction per day requested and stay below 4500.
TX_LIMIT, TX_WINDOW_S = 4500, 600
RETRY_WAITS_S = (10, 30, 60, 120)
KEY_RETRY_WAITS_S = (60, 300, 660)   # "Invalid MAP_KEY" under load: wait past the 10-minute window
MAX_KEY_FAILURES = 3                 # consecutive requests still rejected after all waits -> stop the run


class KeyRejected(SystemExit):
    """Raised to stop a run cleanly when FIRMS keeps rejecting the key."""


# ------------------------------------------------------------------ small helpers
def load_key():
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    env = PROJECT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("FIRMS_MAP_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key


def parse_event_time(value):
    """'2025-01-07T23:00Z' or '2025-01-07' -> aware UTC datetime (day precision -> 00:00Z)."""
    v = value.strip()
    if "T" in v:
        return datetime.strptime(v.rstrip("Z"), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    return datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def box_around(lat, lon, half_km):
    dlat = half_km / 111.32
    dlon = half_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat          # west, south, east, north


def union_box(*boxes):
    boxes = [b for b in boxes if b]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def fmt_box(b):
    w, s, e, n = (round(x, 3) for x in b)
    return f"{max(w, -180)},{max(s, -90)},{min(e, 180)},{min(n, 90)}"


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ------------------------------------------------------------------ FIRMS client
class Firms:
    def __init__(self, key, offline=False):
        self.key, self.offline = key, offline
        self.spent = deque()                    # (timestamp, transactions)
        self.log = []
        self.key_failures = 0                   # consecutive requests that ended in a key rejection

    def _wait_for_budget(self, cost):
        while True:
            now = time.time()
            while self.spent and now - self.spent[0][0] > TX_WINDOW_S:
                self.spent.popleft()
            used = sum(c for _, c in self.spent)
            if used + cost <= TX_LIMIT:
                return
            pause = TX_WINDOW_S - (now - self.spent[0][0]) + 1
            print(f"    transaction budget: {used} used in the last 10 min, pausing {pause:.0f}s", flush=True)
            time.sleep(pause)

    def _get(self, url_path):
        """GET API + url_path (the key is inside url_path and never printed or logged).

        "Invalid MAP_KEY" is treated as temporary: in the first full run FIRMS answered that way
        for hundreds of requests in a row while the same key worked before and after. It is
        retried with long waits; if it keeps happening the run stops instead of silently
        recording 'no data' for the rest of the events."""
        busy_waits, key_waits = iter(RETRY_WAITS_S), iter(KEY_RETRY_WAITS_S)
        wait = 0
        while True:
            if wait:
                wait += random.uniform(0, wait * 0.2)
                print(f"    waiting {wait:.0f}s before retry", flush=True)
                time.sleep(wait)
            try:
                r = requests.get(API + url_path, headers={"User-Agent": USER_AGENT}, timeout=(15, 120))
            except requests.RequestException as exc:
                print(f"    no response ({type(exc).__name__}); will retry", flush=True)
                wait = next(busy_waits, None)
                if wait is None:
                    return None, "gave_up"
                continue
            body = r.text
            head = body[:200].lower()
            if "invalid map_key" in head or "invalid map key" in head:
                print("    FIRMS rejected the map key (seen to be temporary under load)", flush=True)
                wait = next(key_waits, None)
                if wait is None:
                    self.key_failures += 1
                    if self.key_failures >= MAX_KEY_FAILURES:
                        raise KeyRejected(
                            f"FIRMS rejected the map key for {MAX_KEY_FAILURES} requests in a row, even after waiting. "
                            "Stopping so no more events are recorded as 'no data'. Check the key at "
                            "https://firms.modaps.eosdis.nasa.gov/api/map_key/ and rerun the same command later: "
                            "everything downloaded so far is cached.")
                    return None, "key_rejected"
                continue
            self.key_failures = 0
            if r.status_code == 200 and "exceed" not in head:
                return body, None
            if r.status_code in (429, 500, 502, 503, 504) or "exceed" in head:
                print(f"    HTTP {r.status_code}: busy or over limit", flush=True)
                wait = next(busy_waits, None)
                if wait is None:
                    return None, "gave_up"
                continue
            return None, f"http_{r.status_code}: {body[:120].strip()}"

    def key_status(self):
        if self.offline:
            return
        body, err = self._get(f"/mapserver/mapkey_status/?MAP_KEY={self.key}")
        if err:
            print(f"map key status: unavailable ({err})")
            return
        try:
            s = json.loads(body)
            print(f"map key status: {s.get('current_transactions')} of {s.get('transaction_limit')} "
                  f"transactions used per {s.get('transaction_interval')}")
        except ValueError:
            print("map key status: " + body.strip()[:120])

    def availability(self):
        """{product: (min_date, max_date)} from /api/data_availability; cached for the day."""
        path = CACHE / f"availability_{date.today().isoformat()}.csv"
        if path.exists():
            text = path.read_text(encoding="utf-8")
        else:
            olds = sorted(CACHE.glob("availability_*.csv"))
            if self.offline:
                if not olds:
                    sys.exit("offline: no cached data availability file - run once online first")
                text = olds[-1].read_text(encoding="utf-8")
            else:
                text, err = self._get(f"/api/data_availability/csv/{self.key}/ALL")
                if text is None or "data_id" not in text:
                    sys.exit(f"could not read FIRMS data availability ({err or text[:120]!r}). "
                             "Without it, 'no detection' cannot be told apart from 'no data' - stopping.")
                CACHE.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        avail = {}
        for row in csv.DictReader(io.StringIO(text)):
            try:
                avail[row["data_id"].strip()] = (date.fromisoformat(row["min_date"].strip()),
                                                 date.fromisoformat(row["max_date"].strip()))
            except (KeyError, ValueError):
                continue
        wanted = {p for pair in SENSORS.values() for p in pair if p}
        if not wanted & set(avail):
            sys.exit("data availability lists none of the expected products "
                     f"({', '.join(sorted(wanted))}); found: {', '.join(sorted(avail)) or 'nothing'}")
        return avail

    def area(self, product, box, start, n_days, label):
        """Detections for product in box for [start, start + n_days - 1]. Returns (rows, status)."""
        bstr = fmt_box(box)
        path = CACHE / f"{product}_{bstr.replace(',', '_')}_{start.isoformat()}_{n_days}.csv"
        if path.exists():
            text, status = path.read_text(encoding="utf-8"), "cache"
        elif self.offline:
            self.log.append(dict(label=label, product=product, box=bstr, start=start, days=n_days,
                                 status="not_cached", n_rows=""))
            return None, "not_cached"
        else:
            self._wait_for_budget(n_days)
            text, err = self._get(f"/api/area/csv/{self.key}/{product}/{bstr}/{n_days}/{start.isoformat()}")
            self.spent.append((time.time(), n_days))
            if text is not None and text.strip() == "":
                text = ""                       # HTTP 200 with an empty body: treat as zero detections
            elif text is None or not text.lstrip().lower().startswith("latitude"):
                # An error message is not data: never cache it.
                status = err or f"bad_response: {(text or '')[:80].strip()}"
                self.log.append(dict(label=label, product=product, box=bstr, start=start, days=n_days,
                                     status=status, n_rows=""))
                return None, status
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            status = "live"
        rows = list(csv.DictReader(io.StringIO(text)))
        self.log.append(dict(label=label, product=product, box=bstr, start=start, days=n_days,
                             status=status, n_rows=len(rows)))
        return rows, status


def plan_requests(sensor, first, last, avail):
    """Split [first, last] into (product, start, n_days) runs, SP where available, else NRT.

    Returns (runs, gap_days)."""
    sp, nrt = SENSORS[sensor]
    def product_for(d):
        for p in (sp, nrt):
            if p and p in avail and avail[p][0] <= d <= avail[p][1]:
                return p
        return None
    runs, gaps, cur = [], [], None
    for d in daterange(first, last):
        p = product_for(d)
        if p is None:
            gaps.append(d)
            cur = None
            continue
        if cur and cur[0] == p and cur[2] < MAX_DAY_RANGE and cur[1] + timedelta(days=cur[2]) == d:
            cur[2] += 1
        else:
            cur = [p, d, 1]
            runs.append(cur)
    return [tuple(r) for r in runs], gaps


# ------------------------------------------------------------------ measuring
def local_projection(lat, lon):
    crs = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m"
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform


def load_footprints(path):
    if not path.exists():
        return {}
    gj = json.loads(path.read_text(encoding="utf-8"))
    return {f["properties"]["site_id"]: shape(f["geometry"]) for f in gj.get("features", [])}


def to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def measure(ev, det, footprint, fwd):
    """Distances and timing of one detection relative to one event."""
    lat, lon = float(det["latitude"]), float(det["longitude"])
    scan, track = to_float(det.get("scan"), 0.375), to_float(det.get("track"), 0.375)
    pixel_half = 0.5 * math.hypot(scan, track)
    t = det.get("acq_time", "").strip().zfill(4)
    acq = datetime.strptime(det["acq_date"].strip() + t, "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc)
    out = {
        "acq_utc": acq.strftime("%Y-%m-%dT%H:%MZ"),
        "hours_from_event": round((acq - ev["_t0"]).total_seconds() / 3600, 1),
        "pixel_half_km": round(pixel_half, 3),
        "dist_site_km": "", "dist_footprint_km": "", "site_match": "",
        "dist_coarse_km": "", "coarse_match": "",
    }
    if ev["lat_site"]:
        d = haversine_km(lat, lon, float(ev["lat_site"]), float(ev["lon_site"]))
        out["dist_site_km"] = round(d, 3)
        if footprint is not None:
            p = transform(fwd, Point(lon, lat))
            dfp = transform(fwd, footprint).distance(p) / 1000
            out["dist_footprint_km"] = round(dfp, 3)
            out["site_match"] = dfp <= pixel_half + SITE_MATCH_BUFFER_KM
        else:
            unc = to_float(ev.get("site_uncertainty_km"), 1.0) or 1.0
            out["site_match"] = d <= unc + pixel_half + SITE_MATCH_BUFFER_KM
    if ev["lat_coarse"]:
        dc = haversine_km(lat, lon, float(ev["lat_coarse"]), float(ev["lon_coarse"]))
        out["dist_coarse_km"] = round(dc, 3)
        out["coarse_match"] = dc <= COARSE_MATCH_KM
    return out


def event_box(ev):
    boxes = []
    if ev["lat_site"]:
        unc = to_float(ev.get("site_uncertainty_km"), 0) or 0
        boxes.append(box_around(float(ev["lat_site"]), float(ev["lon_site"]), max(SITE_BOX_MIN_KM, unc + 5)))
    if ev["lat_coarse"]:
        boxes.append(box_around(float(ev["lat_coarse"]), float(ev["lon_coarse"]), COARSE_BOX_KM))
    return union_box(*boxes) if boxes else None


def baseline_contamination(ev, events):
    """Other events at the same site whose day falls in this event's baseline window."""
    d0 = ev["_t0"].date()
    lo, hi = d0 + timedelta(days=BASELINE_WINDOW[0]), d0 + timedelta(days=BASELINE_WINDOW[1])
    hits = [o["event_id"] for o in events
            if o is not ev and o["site_id"] == ev["site_id"] and o["category"] != "negative_control"
            and lo <= o["_t0"].date() <= hi]
    return ";".join(hits)


# ------------------------------------------------------------------ main
def cmd_run(args):
    key = load_key()
    if not key and not args.offline:
        sys.exit("no FIRMS map key: set FIRMS_MAP_KEY or put FIRMS_MAP_KEY=... in " + str(PROJECT / ".env"))
    client = Firms(key, offline=args.offline)
    client.key_status()
    avail = client.availability()
    sensors = args.sensors or DEFAULT_SENSORS
    footprints = load_footprints(Path(args.footprints))

    with open(args.events, newline="", encoding="utf-8") as f:
        events = list(csv.DictReader(f))
    for ev in events:
        ev["_t0"] = parse_event_time(ev["event_time_utc"])
    todo = [e for e in events if not args.only or e["event_id"] in args.only]

    det_rows, summaries = [], []
    for ev in todo:
        box = event_box(ev)
        print(f"{ev['event_id']} {ev['site_id']} {ev['place_name'][:50]} {ev['event_time_utc']}", flush=True)
        fp = footprints.get(ev["site_id"])
        ref_lat = float(ev["lat_site"] or ev["lat_coarse"]); ref_lon = float(ev["lon_site"] or ev["lon_coarse"])
        fwd = local_projection(ref_lat, ref_lon)
        d0 = ev["_t0"].date()
        windows = [("event", EVENT_WINDOW)] + ([] if args.no_baseline else [("baseline", BASELINE_WINDOW)])
        coverage, gaps_all, failed = defaultdict(list), defaultdict(list), []
        seen = set()
        for wname, (a, b) in windows:
            first, last = d0 + timedelta(days=a), d0 + timedelta(days=b)
            for sensor in sensors:
                runs, gaps = plan_requests(sensor, first, last, avail)
                if gaps:
                    gaps_all[wname].append(f"{sensor}:{len(gaps)}d")
                for product, start, n in runs:
                    rows, status = client.area(product, box, start, n, f"{ev['event_id']}:{wname}")
                    if rows is None:
                        failed.append(f"{product}@{start}:{status}")
                        continue
                    coverage[wname].append(f"{product}:{start.isoformat()}+{n}d")
                    for det in rows:
                        k = (det.get("latitude"), det.get("longitude"), det.get("acq_date"),
                             det.get("acq_time"), det.get("satellite"), wname)
                        if k in seen:
                            continue
                        seen.add(k)
                        m = measure(ev, det, fp, fwd)
                        det_rows.append({
                            "event_id": ev["event_id"], "site_id": ev["site_id"], "category": ev["category"],
                            "window": wname, "product": product, "satellite": det.get("satellite", ""),
                            "latitude": det["latitude"], "longitude": det["longitude"],
                            **m,
                            "frp_mw": det.get("frp", ""), "confidence": det.get("confidence", ""),
                            "daynight": det.get("daynight", ""), "scan_km": det.get("scan", ""),
                            "track_km": det.get("track", ""), "type": det.get("type", ""),
                        })
        mine = [r for r in det_rows if r["event_id"] == ev["event_id"]]
        def pick(win, col):
            return [r for r in mine if r["window"] == win and r[col] is True]
        site_ev, site_bl = pick("event", "site_match"), pick("baseline", "site_match")
        co_ev, co_bl = pick("event", "coarse_match"), pick("baseline", "coarse_match")
        bl_days = BASELINE_WINDOW[1] - BASELINE_WINDOW[0] + 1
        ev_days = EVENT_WINDOW[1] - EVENT_WINDOW[0] + 1
        def first_h(rows):
            # Earliest matching detection relative to the event time. Negative = heat was already
            # there before the reported time (or the event time / local date is off).
            hours = [r["hours_from_event"] for r in rows]
            return min(hours) if hours else ""
        summaries.append({
            "event_id": ev["event_id"], "site_id": ev["site_id"], "category": ev["category"],
            "place_name": ev["place_name"], "event_time_utc": ev["event_time_utc"],
            "time_precision": ev["time_precision"], "ground_truth": ev["ground_truth"],
            "site_level": "yes" if ev["lat_site"] else "not_assessable_no_site",
            "footprint": "yes" if fp is not None else "no",
            "event_coverage": " ".join(coverage["event"]) or "NONE",
            "event_gaps": " ".join(gaps_all["event"]),
            "failed_requests": " ".join(failed),
            "n_site_event": len(site_ev) if ev["lat_site"] else "",
            "n_site_baseline_per_5d": round(len(site_bl) * ev_days / bl_days, 2) if ev["lat_site"] and not args.no_baseline else "",
            "site_first_hours": first_h(site_ev) if ev["lat_site"] else "",
            "site_max_frp_mw": max((to_float(r["frp_mw"], 0) for r in site_ev), default="") if ev["lat_site"] else "",
            "n_coarse_event": len(co_ev),
            "n_coarse_baseline_per_5d": round(len(co_bl) * ev_days / bl_days, 2) if not args.no_baseline else "",
            "coarse_nearest_km": min((r["dist_coarse_km"] for r in mine if r["window"] == "event"), default=""),
            "baseline_contaminated_by": baseline_contamination(ev, events),
        })
        s = summaries[-1]
        print(f"    site: {s['n_site_event']} in window (baseline {s['n_site_baseline_per_5d']}/5d)   "
              f"coarse: {s['n_coarse_event']} (baseline {s['n_coarse_baseline_per_5d']}/5d)"
              + (f"   FAILED {len(failed)}" if failed else ""), flush=True)

    OUT.mkdir(exist_ok=True)
    write_csv(OUT / "firms_detections.csv", det_rows)
    write_csv(OUT / "firms_event_summary.csv", summaries)
    write_csv(OUT / "firms_requests.csv", client.log)
    live = sum(1 for r in client.log if r["status"] == "live")
    bad = [r for r in client.log if r["status"] not in ("live", "cache")]
    print(f"\n{len(summaries)} events, {len(det_rows)} detections, {live} live requests, "
          f"{len(bad)} failed - outputs in {OUT}")
    incomplete = [s["event_id"] for s in summaries if s["failed_requests"]]
    if incomplete:
        print("\n" + "!" * 72)
        print(f"WARNING: {len(incomplete)} events are INCOMPLETE (failed requests): {' '.join(incomplete)}")
        print("Their counts are missing data, not 'nothing seen'. Rerun the same command;")
        print("only the failed requests are fetched again.")
        print("!" * 72)


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--events", default=EVENTS)
    r.add_argument("--footprints", default=FOOTPRINTS)
    r.add_argument("--only", nargs="*", help="event_ids, e.g. E021 E019")
    r.add_argument("--sensors", nargs="*", choices=list(SENSORS), help="default: all four")
    r.add_argument("--no-baseline", action="store_true", help="skip the 30-day baseline window")
    r.add_argument("--offline", action="store_true", help="use cached responses only")
    args = p.parse_args()
    {"run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
