"""Offline test of firms.py with a fake FIRMS server (no network, no real key).

    python tests/test_firms.py
"""
import csv, json, sys, tempfile, types
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import firms as m

def expect(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        expect.failed = True
expect.failed = False

tmp = Path(tempfile.mkdtemp())
m.CACHE, m.OUT = tmp / "cache", tmp / "out"
m.time.sleep = lambda s: None
KEY = "SECRETKEY123"

# ---------------------------------------------------------------- plan_requests
avail = {"VIIRS_SNPP_SP": (date(2012, 1, 20), date(2024, 12, 31)),
         "VIIRS_SNPP_NRT": (date(2024, 10, 1), date(2026, 10, 24)),
         "VIIRS_NOAA21_NRT": (date(2025, 1, 2), date(2026, 10, 24))}
runs, gaps = m.plan_requests("VIIRS_SNPP", date(2024, 12, 30), date(2025, 1, 3), avail)
expect(runs == [("VIIRS_SNPP_SP", date(2024, 12, 30), 2), ("VIIRS_SNPP_NRT", date(2025, 1, 1), 3)] and not gaps,
       f"SP while archived, NRT after ({runs})")
runs, gaps = m.plan_requests("VIIRS_NOAA21", date(2024, 12, 30), date(2025, 1, 3), avail)
expect(runs == [("VIIRS_NOAA21_NRT", date(2025, 1, 2), 2)] and len(gaps) == 3, f"days with no product are gaps ({runs}, {len(gaps)})")
runs, _ = m.plan_requests("VIIRS_SNPP", date(2023, 1, 1), date(2023, 1, 30), avail)
expect([r[2] for r in runs] == [5] * 6, f"30 days split into 5-day requests ({[r[2] for r in runs]})")
runs, gaps = m.plan_requests("VIIRS_NOAA20", date(2023, 1, 1), date(2023, 1, 5), avail)
expect(runs == [] and len(gaps) == 5, "sensor missing from availability -> all gaps, no requests")
expect(m.parse_event_time("2025-01-07T23:00Z").hour == 23 and m.parse_event_time("2025-01-07").hour == 0, "event time parsing")

# ---------------------------------------------------------------- fixtures
SITE = (51.4367, 46.0905)
events = tmp / "events.csv"
cols = ["event_id", "site_id", "category", "place_name", "lat_site", "lon_site", "site_uncertainty_km",
        "lat_coarse", "lon_coarse", "event_time_utc", "time_precision", "ground_truth"]
rows = [
    dict(event_id="E1", site_id="S1", category="large_outdoor_fire", place_name="Depot", lat_site=SITE[0], lon_site=SITE[1],
         site_uncertainty_km="0.4", lat_coarse="51.48389", lon_coarse="46.10528", event_time_utc="2025-01-07T23:00Z",
         time_precision="hour", ground_truth="confirmed"),
    dict(event_id="E2", site_id="S1", category="negative_control", place_name="Depot", lat_site=SITE[0], lon_site=SITE[1],
         site_uncertainty_km="0.4", lat_coarse="51.48389", lon_coarse="46.10528", event_time_utc="2024-01-08",
         time_precision="day", ground_truth="control"),
    dict(event_id="E3", site_id="S9", category="large_outdoor_fire", place_name="No-site depot", lat_site="", lon_site="",
         site_uncertainty_km="", lat_coarse="46.70241", lon_coarse="41.72487", event_time_utc="2024-08-18",
         time_precision="day", ground_truth="confirmed"),
    dict(event_id="E4", site_id="S1", category="large_outdoor_fire", place_name="Depot", lat_site=SITE[0], lon_site=SITE[1],
         site_uncertainty_km="0.4", lat_coarse="51.48389", lon_coarse="46.10528", event_time_utc="2024-12-10",
         time_precision="day", ground_truth="confirmed"),
]
with open(events, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
d = 0.003
fp = tmp / "fp.geojson"
fp.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"site_id": "S1", "osm": "way/1"},
    "geometry": {"type": "Polygon", "coordinates": [[[SITE[1]-d, SITE[0]-d], [SITE[1]+d, SITE[0]-d], [SITE[1]+d, SITE[0]+d],
                                                      [SITE[1]-d, SITE[0]+d], [SITE[1]-d, SITE[0]-d]]]}}]}))

AVAIL_CSV = ("data_id,min_date,max_date\n"
             "VIIRS_SNPP_SP,2012-01-20,2024-12-31\nVIIRS_SNPP_NRT,2024-10-01,2026-10-24\n"
             "VIIRS_NOAA20_NRT,2024-10-01,2026-10-24\nVIIRS_NOAA20_SP,2018-04-01,2024-12-31\n"
             "MODIS_SP,2000-11-01,2024-12-31\nMODIS_NRT,2024-10-01,2026-10-24\n"
             "VIIRS_NOAA21_NRT,2025-01-08,2026-10-24\n")
HDR = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
def det(lat, lon, d, t, sat="N", frp="55.1"):
    return f"{lat},{lon},340.1,0.39,0.36,{d},{t},{sat},VIIRS,h,2.0NRT,290.0,{frp},N\n"

calls = []
def fake_get(url, headers=None, timeout=None):
    calls.append(url)
    R = types.SimpleNamespace
    if "mapkey_status" in url:
        return R(status_code=200, text=json.dumps({"transaction_limit": 5000, "current_transactions": 12, "transaction_interval": "10 minutes"}))
    if "data_availability" in url:
        return R(status_code=200, text=AVAIL_CSV)
    parts = url.split("/")
    product, box, n, start = parts[-4], parts[-3], int(parts[-2]), parts[-1]
    if product == "MODIS_NRT":
        return R(status_code=200, text="Invalid MAP_KEY.")            # must not be cached
    if product == "VIIRS_NOAA20_NRT":
        return R(status_code=200, text="")                            # empty body = zero detections
    body = HDR
    if product == "VIIRS_SNPP_NRT" and start == "2025-01-06":
        body += det(SITE[0] + 0.001, SITE[1], "2025-01-08", "15")                 # inside outline, 00:15Z (1.25 h after)
        body += det(SITE[0] + 0.027, SITE[1], "2025-01-08", "0120")               # ~3 km N: not at site, within 10 km of city
        body += det(SITE[0] + 0.001, SITE[1], "2025-01-08", "15")                 # exact duplicate -> counted once
    if product == "VIIRS_SNPP_SP" and start == "2024-12-01":
        body += det(SITE[0], SITE[1] + 0.001, "2024-12-05", "2310", frp="3.0")    # baseline heat at the site (a flare?)
    if product == "VIIRS_SNPP_SP" and start == "2024-08-17":
        body += det(46.70, 41.72, "2024-08-18", "2250")                           # near coarse point of E3
    return R(status_code=200, text=body)

m.requests.get = fake_get
m.load_key = lambda: KEY
args = types.SimpleNamespace(events=events, footprints=fp, only=None, sensors=None, no_baseline=False, offline=False)
m.cmd_run(args)

S = {r["event_id"]: r for r in csv.DictReader(open(m.OUT / "firms_event_summary.csv", encoding="utf-8"))}
D = list(csv.DictReader(open(m.OUT / "firms_detections.csv", encoding="utf-8")))
L = list(csv.DictReader(open(m.OUT / "firms_requests.csv", encoding="utf-8")))

e1 = S["E1"]
expect(e1["n_site_event"] == "1", f"E1: one detection inside the outline, duplicate counted once (got {e1['n_site_event']})")
expect(e1["site_first_hours"] in ("1.2", "1.3", "1.25"), f"E1: acq_time '15' read as 00:15Z -> ~1.2 h after event (got {e1['site_first_hours']})")
expect(e1["n_coarse_event"] == "2", f"E1: both event detections within 10 km of the city point (got {e1['n_coarse_event']})")
expect("VIIRS_SNPP_NRT" in e1["event_coverage"] and "VIIRS_NOAA20_NRT" in e1["event_coverage"],
       "E1: empty NOAA-20 response still counts as coverage (zero detections, not missing)")
expect("MODIS_NRT" in e1["failed_requests"] and "MODIS_NRT" not in e1["event_coverage"],
       "E1: error response reported as failed, not as coverage")
expect("VIIRS_NOAA21" in e1["event_gaps"], f"E1: NOAA-21 days before its start listed as gaps ({e1['event_gaps']})")
expect(e1["baseline_contaminated_by"] == "E4", f"E1: strike E4 inside E1's baseline window flagged (got {e1['baseline_contaminated_by']!r})")
expect(float(e1["n_site_baseline_per_5d"]) > 0, "E1: baseline heat at the site counted")
expect(e1["footprint"] == "yes", "E1 uses the outline")
e3 = S["E3"]
expect(e3["site_level"] == "not_assessable_no_site" and e3["n_site_event"] == "", "E3: no site -> site level not assessable")
expect(e3["n_coarse_event"] == "1", f"E3: coarse-level detection counted (got {e3['n_coarse_event']})")
expect(S["E2"]["baseline_contaminated_by"] == "", "E2 control: no contamination")
far = [r for r in D if r["event_id"] == "E1" and r["window"] == "event" and float(r["dist_site_km"]) > 2]
expect(far and far[0]["site_match"] == "False" and 2.5 < float(far[0]["dist_footprint_km"]) < 3.2,
       f"3 km detection measured to the outline, not matched ({far[0]['dist_footprint_km'] if far else None})")

cached = [p.name for p in m.CACHE.iterdir()]
expect(not any(KEY in n for n in cached), "map key never in cache file names")
expect(not any("MODIS_NRT" in n for n in cached), "error responses not cached")
expect(not any(KEY in json.dumps(r) for r in L), "map key never in the request log")
n_live = sum(1 for r in L if r["status"] == "live")

# offline rerun from cache reproduces the summary with no network calls
calls.clear()
m.requests.get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used offline"))
m.cmd_run(types.SimpleNamespace(**{**vars(args), "offline": True}))
S2 = {r["event_id"]: r for r in csv.DictReader(open(m.OUT / "firms_event_summary.csv", encoding="utf-8"))}
expect(S2["E1"]["n_site_event"] == "1" and S2["E3"]["n_coarse_event"] == "1", "offline rerun gives the same counts")
expect("not_cached" in S2["E1"]["failed_requests"], "offline: the never-cached MODIS_NRT request shows as not_cached")

# budget: pauses when the 10-minute window would be exceeded
slept = []
m.time.sleep = lambda s: slept.append(s)
c = m.Firms(KEY)
now = m.time.time()
c.spent.extend([(now - 30, 4400)])
real_time = m.time.time
t = [now]
m.time.time = lambda: t[0]
def advance(s):
    slept.append(s); t[0] += s
m.time.sleep = advance
c._wait_for_budget(200)
m.time.time = real_time
expect(slept and 560 < slept[0] < 580, f"over budget -> waits until the oldest batch leaves the 10-min window ({slept[:1]})")

# "Invalid MAP_KEY" is retried: two rejections then data -> success
m.time.sleep = lambda s: None
seq = [types.SimpleNamespace(status_code=400, text="Invalid MAP_KEY."),
       types.SimpleNamespace(status_code=200, text="Invalid MAP_KEY."),
       types.SimpleNamespace(status_code=200, text=HDR)]
m.requests.get = lambda *a, **k: seq.pop(0)
c2 = m.Firms(KEY)
body, err = c2._get("/api/area/csv/X/VIIRS_SNPP_SP/1,1,2,2/1/2025-01-01")
expect(err is None and body.startswith("latitude"), "temporary key rejection retried until data arrives")
# persistent rejection: after MAX_KEY_FAILURES requests in a row the run stops instead of recording gaps
m.requests.get = lambda *a, **k: types.SimpleNamespace(status_code=400, text="Invalid MAP_KEY.")
c3 = m.Firms(KEY)
stopped = False
try:
    for _ in range(m.MAX_KEY_FAILURES + 1):
        c3._get("/x")
except SystemExit as e:
    stopped = "rejected the map key" in str(e)
expect(stopped and c3.key_failures == m.MAX_KEY_FAILURES, "persistent key rejection stops the run with a message")

print(f"\n{n_live} live requests in the fake run")
sys.exit(1 if expect.failed else 0)
