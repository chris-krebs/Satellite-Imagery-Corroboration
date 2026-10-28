"""Offline test of persistent_heat.py with a fake FIRMS history (no network, no key).

    python tests/test_persistent_heat.py
"""
import csv, json, sys, tempfile, types
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import persistent_heat as m
firms = m.firms

def expect(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        expect.failed = True
expect.failed = False

tmp = Path(tempfile.mkdtemp())
m.CACHE, m.OUT = tmp / "cache", tmp / "out"
firms.time.sleep = lambda s: None

# ---------------------------------------------------------------- pure functions
expect(m.parse_incident_date("2024-02") == (date(2024, 2, 1), date(2024, 2, 29)), "month-only incident = whole month (leap year)")
expect(m.parse_incident_date("2025-01-24") == (date(2025, 1, 24),) * 2, "day incident")
ev = [{"event_time_utc": "2025-01-07T23:00Z", "category": "large_outdoor_fire"},
      {"event_time_utc": "2024-01-08", "category": "negative_control"}]
start, end, excl = m.history_plan(ev, [m.parse_incident_date("2024-06")])
expect(end == date(2024, 6, 1) - timedelta(days=60) and (end - start).days == 364,
       f"history = 365 days ending 60 days before earliest incident ({start}..{end})")
expect(date(2024, 1, 8) in excl and date(2023, 12, 31) in excl and date(2024, 1, 18) in excl and date(2024, 1, 19) not in excl,
       "control window (-8..+10 days) excluded from history even though controls are not incidents")
segs = m.included_segments(start, end, excl)
expect(len(segs) == 2 and segs[0][1] == date(2023, 12, 30) and segs[1][0] == date(2024, 1, 19), f"history split around the hole ({segs})")

# ---------------------------------------------------------------- fixtures
SITE = (55.0, 40.0)
FLARE = (SITE[0] + 0.002, SITE[1])              # ~220 m north of the site point
NEW = (SITE[0] - 0.012, SITE[1] + 0.004)        # ~1.4 km SSE, no history there
events = tmp / "events.csv"
cols = ["event_id", "site_id", "category", "place_name", "lat_site", "lon_site", "site_uncertainty_km",
        "lat_coarse", "lon_coarse", "event_time_utc", "time_precision", "ground_truth"]
base = dict(site_id="S1", place_name="Refinery", lat_site=SITE[0], lon_site=SITE[1], site_uncertainty_km="1.5",
            lat_coarse="55.1", lon_coarse="40.1", time_precision="day")
rows = [dict(base, event_id="E1", category="large_outdoor_fire", event_time_utc="2025-01-07", ground_truth="confirmed"),
        dict(base, event_id="E2", category="negative_control", event_time_utc="2024-01-08", ground_truth="control"),
        dict(base, event_id="E3", category="large_outdoor_fire", event_time_utc="2025-03-01", ground_truth="confirmed"),
        dict(base, event_id="E9", site_id="S9", category="port_ship_fire", place_name="Ship", lat_site="14.0", lon_site="41.0",
             site_uncertainty_km="20", event_time_utc="2024-08-23", ground_truth="confirmed")]
with open(events, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
inc = tmp / "incidents.csv"
inc.write_text("site_id,date,source,note\nS1,2024-06,x,month-only strike\n", encoding="utf-8")
fp = tmp / "fp.geojson"
fp.write_text(json.dumps({"type": "FeatureCollection", "features": []}))

dcols = ["event_id", "site_id", "category", "window", "product", "satellite", "latitude", "longitude", "acq_utc",
         "hours_from_event", "pixel_half_km", "dist_site_km", "dist_footprint_km", "site_match", "dist_coarse_km",
         "coarse_match", "frp_mw", "confidence", "daynight", "scan_km", "track_km", "type"]
def drow(eid, lat, lon, frp, t="2025-01-08T01:00Z", sat="N", match="True"):
    d = firms.haversine_km(lat, lon, *SITE)
    return dict(event_id=eid, site_id="S1", category="", window="event", product="VIIRS_SNPP_SP", satellite=sat,
                latitude=lat, longitude=lon, acq_utc=t, hours_from_event="25.0", pixel_half_km="0.27",
                dist_site_km=round(d, 3), dist_footprint_km="", site_match=match, dist_coarse_km="", coarse_match="",
                frp_mw=frp, confidence="n", daynight="N", scan_km="0.39", track_km="0.36", type="0")
cl = [(FLARE[0] + dy, FLARE[1] + dx) for dy, dx in ((0.0035, 0), (-0.0035, 0), (0, 0.006), (0, -0.006))]
det_rows = [
    drow("E1", *FLARE, 5.0),                                   # flare, usual strength -> background
    drow("E1", *FLARE, 60.0, t="2025-01-08T11:00Z"),           # flare cell, 60 MW -> anomalous (FRP)
    drow("E1", *NEW, 8.0, t="2025-01-08T13:00Z"),              # no history -> new
    drow("E1", SITE[0] + 0.036, SITE[1], 9.0, match="False"),  # 4 km away: labelled, not counted at site
    drow("E2", *FLARE, 6.0, t="2024-01-09T01:00Z"),            # control: only the flare -> background
] + [drow("E3", la, lo, 4.0, t="2025-03-02T01:00Z") for la, lo in cl]   # 4 cells at once near the flare -> cluster
dets = tmp / "dets.csv"
with open(dets, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=dcols); w.writeheader(); w.writerows(det_rows)

AVAIL = ("data_id,min_date,max_date\nVIIRS_SNPP_SP,2012-01-20,2026-06-30\nVIIRS_NOAA20_SP,2018-04-01,2026-06-30\n"
         "VIIRS_SNPP_NRT,2026-07-01,2026-10-25\nVIIRS_NOAA20_NRT,2026-07-01,2026-10-25\n")
HDR = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight,type\n"
requested_days = set()
def fake_get(url, headers=None, timeout=None):
    R = types.SimpleNamespace
    if "mapkey_status" in url:
        return R(status_code=200, text='{"transaction_limit":5000,"current_transactions":0,"transaction_interval":"10 minutes"}')
    if "data_availability" in url:
        return R(status_code=200, text=AVAIL)
    parts = url.split("/")
    product, n, start = parts[-4], int(parts[-2]), date.fromisoformat(parts[-1])
    body = HDR
    for k in range(n):
        d = start + timedelta(days=k)
        requested_days.add(d)
        if product == "VIIRS_SNPP_SP" and d.day in (3, 17):      # flare twice a month, every month
            body += f"{FLARE[0]},{FLARE[1]},330,0.39,0.36,{d.isoformat()},0105,N,VIIRS,n,2.0SP,290,5.5,N,0\n"
        if product == "VIIRS_SNPP_SP" and d == date(2025, 2, 10):  # sanity case: big fire in the flare cell
            body += f"{FLARE[0]},{FLARE[1]},360,0.39,0.36,{d.isoformat()},0105,N,VIIRS,h,2.0SP,300,80.0,N,0\n"
            body += f"{FLARE[0]},{FLARE[1]},330,0.39,0.36,{d.isoformat()},1300,N,VIIRS,n,2.0SP,290,5.0,D,0\n"
    return R(status_code=200, text=body)
firms.requests.get = fake_get
firms.load_key = lambda: "KEY"

args = types.SimpleNamespace(events=events, footprints=fp, detections=dets, incidents=inc, only=None, offline=False)
m.cmd_run(args)

S = {r["event_id"]: r for r in csv.DictReader(open(m.OUT / "heat_event_summary.csv", encoding="utf-8"))}
L = list(csv.DictReader(open(m.OUT / "labelled_detections.csv", encoding="utf-8")))
C = list(csv.DictReader(open(m.OUT / "heat_cells.csv", encoding="utf-8")))
H = list(csv.DictReader(open(m.OUT / "heat_history.csv", encoding="utf-8")))

expect(not any(date(2023, 12, 31) <= d <= date(2024, 1, 18) for d in requested_days), "no history requested inside the control's window")
expect(not any(d > date(2024, 4, 2) for d in requested_days), "no history requested after the history end")
expect(H and H[0]["site_id"] == "S1" and len(H) == 1, "ship with 20 km uncertainty skipped")
flare_cells = [c for c in C if c["persistent"] == "True"]
expect(len(flare_cells) == 1 and int(flare_cells[0]["n_months"]) >= 11, f"flare cell found as persistent ({[(c['cell'], c['n_days'], c['n_months']) for c in C]})")
lab = {(r["event_id"], r["frp_mw"], r["acq_utc"]): r["label"] for r in L}
expect(lab[("E1", "5.0", "2025-01-08T01:00Z")] == "background", "flare at usual strength -> background")
expect(lab[("E1", "60.0", "2025-01-08T11:00Z")] == "anomalous", "flare cell at 60 MW -> anomalous (FRP)")
expect(lab[("E1", "8.0", "2025-01-08T13:00Z")] == "new", "detection with no history nearby -> new")
expect(S["E1"]["n_site_raw"] == "3" and S["E1"]["n_site_signal"] == "2", f"E1: 3 raw at site, 2 signal (got {S['E1']['n_site_raw']}, {S['E1']['n_site_signal']})")
expect(S["E2"]["n_site_raw"] == "1" and S["E2"]["n_site_signal"] == "0", "E2 control: raw false alarm removed by the filter")
e3 = [r for r in L if r["event_id"] == "E3"]
expect(e3 and all(r["label"] == "anomalous" and "cells lit" in r["reason"] for r in e3),
       f"E3: four cells at once near the flare -> anomalous cluster ({[r['label'] + ':' + r['reason'] for r in e3][:2]})")
expect(any(r["site_match"] == "False" and r["label"] for r in L if r["event_id"] == "E1"), "far detection labelled but not counted at site")
gj = json.loads((m.OUT / "heat_cells.geojson").read_text())
expect(len(gj["features"]) == len(C), "cells exported as GeoJSON squares for QGIS")

# merging: a pre-existing row for another site survives a --only run, S1 rows are replaced not duplicated
with open(m.OUT / "heat_event_summary.csv", "a", newline="", encoding="utf-8") as f:
    f.write("EX1,S77,test,negative_control,control,Elsewhere,2023-01-01,0,0,0,0,0,,,,,0,0\n")
m.cmd_run(types.SimpleNamespace(**{**vars(args), "only": ["S1"]}))
S3 = list(csv.DictReader(open(m.OUT / "heat_event_summary.csv", encoding="utf-8")))
expect(sum(r["site_id"] == "S1" for r in S3) == 3 and any(r["event_id"] == "EX1" for r in S3),
       "--only run replaces its own site and keeps other sites' rows")

# sanity check: a known fire in the flare cell must be kept; never written into the evaluation summary
cases = tmp / "sanity.csv"
cases.write_text("case_id,site_id,date,time_precision,source,note\nSAN1,S1,2025-02-10,day,x,known big fire\nSAN9,S404,2025-02-10,day,x,no such site\n",
                 encoding="utf-8")
m.cmd_sanity(types.SimpleNamespace(events=events, footprints=fp, incidents=inc, cases=cases, offline=False))
SAN = list(csv.DictReader(open(m.OUT / "sanity_summary.csv", encoding="utf-8")))
expect(len(SAN) == 1 and SAN[0]["case_id"] == "SAN1", "unknown site in sanity list skipped")
expect(SAN[0]["n_site_raw"] == "2" and SAN[0]["n_site_signal"] == "1" and SAN[0]["outcome"] == "kept",
       f"sanity: 80 MW in the flare cell kept, the 5 MW pass is background ({SAN[0]['n_site_raw']}, {SAN[0]['n_site_signal']}, {SAN[0]['outcome']})")
expect(not any(r["event_id"].startswith("SAN") for r in csv.DictReader(open(m.OUT / "heat_event_summary.csv", encoding="utf-8"))),
       "sanity cases never appear in the evaluation summary")

# offline rerun gives the same answer
firms.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used offline"))
m.cmd_run(types.SimpleNamespace(**{**vars(args), "offline": True}))
S2 = {r["event_id"]: r for r in csv.DictReader(open(m.OUT / "heat_event_summary.csv", encoding="utf-8"))}
expect(S2["E1"]["n_site_signal"] == "2" and S2["E2"]["n_site_signal"] == "0", "offline rerun reproduces the labels")
sys.exit(1 if expect.failed else 0)
