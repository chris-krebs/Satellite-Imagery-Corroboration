"""Offline test of sentinel2.py with synthetic GeoTIFF scenes and a fake STAC search (no network).

    python tests/test_sentinel2.py
"""
import csv, json, sys, tempfile, types
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform as shp_transform

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import sentinel2 as m

def expect(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        expect.failed = True
expect.failed = False

tmp = Path(tempfile.mkdtemp())
m.CACHE, m.OUT, m.ROOT = tmp / "cache", tmp / "out", tmp
SITE = (55.0, 40.0)                                   # lat, lon -> UTM zone 37N
EPSG = 32637
to_utm = Transformer.from_crs("EPSG:4326", EPSG, always_xy=True).transform
to_ll = Transformer.from_crs(EPSG, "EPSG:4326", always_xy=True).transform
sx, sy = to_utm(SITE[1], SITE[0])
site_utm = box(sx - 300, sy - 300, sx + 300, sy + 300)   # 600 m square "depot"
footprint = shp_transform(to_ll, site_utm)

# source rasters: 20 m pixels over 12 km, in UTM (like real Sentinel-2 tiles)
RES, HALF = 20.0, 6000.0
W = H = int(2 * HALF / RES)
tr = from_origin(sx - HALF, sy + HALF, RES, RES)
xs = sx - HALF + (np.arange(W) + 0.5) * RES
ys = sy + HALF - (np.arange(H) + 0.5) * RES
X, Y = np.meshgrid(xs, ys)
in_site = (abs(X - sx) < 300) & (abs(Y - sy) < 300)
water = (X - sx > 700) & (X - sx < 1100) & (abs(Y - sy) < 2000)          # dark water strip inside the ring
dark_land = (X - sx > 1100) & (X - sx < 1200) & (abs(Y - sy) < 2000)     # dark pixels SCL calls land

def write_scene(name, scl_site, scl_other, burnt):
    files = {}
    for band in ("B02", "B03", "B04", "B8A", "B11", "B12", "SCL"):
        # DN carries a +1000 offset like Collection 1 (reflectance = DN * 1e-4 - 0.1)
        if band == "SCL":
            a = np.where(in_site, scl_site, scl_other)
            a = np.where(water & (scl_other == 4), 6, a).astype(np.uint8)
        elif band == "B8A":
            a = np.where(in_site & burnt, 2500, 4000)
            a = np.where(water | dark_land, 1003, a).astype(np.uint16)       # reflectance ~0.0003
        elif band == "B12":
            a = np.where(in_site & burnt, 3500, 2000)
            a = np.where(water | dark_land, 998, a).astype(np.uint16)        # reflectance slightly negative
        else:
            a = np.full((H, W), 1900, np.uint16)
        p = tmp / "tiles" / f"{name}_{band}.tif"
        p.parent.mkdir(exist_ok=True)
        with rasterio.open(p, "w", driver="GTiff", width=W, height=H, count=1, dtype=a.dtype, crs=f"EPSG:{EPSG}",
                           transform=tr, nodata=0) as dst:
            dst.write(a, 1)
        files[band] = str(p)
    return files

tile_ll = shp_transform(to_ll, box(sx - HALF, sy - HALF, sx + HALF, sy + HALF))
def item(name, d, files, geom=tile_ll, collection="sentinel-2-c1-l2a", old_style=False):
    keymap = {"B02": "blue", "B03": "green", "B04": "red", "B8A": "nir08", "B11": "swir16", "B12": "swir22", "SCL": "scl"}
    rb = [{"scale": 0.0001}] if old_style else [{"scale": 0.0001, "offset": -0.1}]      # old style: no offset in metadata
    assets = {keymap[b]: {"href": f, "raster:bands": rb if b != "SCL" else [{}]} for b, f in files.items()}
    props = {"datetime": f"{d.isoformat()}T09:00:00Z"}
    if old_style:
        props.update({"s2:processing_baseline": "04.00", "earthsearch:boa_offset_applied": False})
    return {"id": name, "collection": collection, "geometry": mapping(geom), "properties": props, "assets": assets}

D0 = date(2025, 1, 7)
DC = date(2025, 8, 1)                                  # control date
items = [
    item("before_clear", D0 - timedelta(days=12), write_scene("bclear", 4, 4, False)),
    item("before_cloudy", D0 - timedelta(days=5), write_scene("bcloudy", 9, 9, False)),     # newest, but cloudy -> skipped
    item("after_smoke", D0 + timedelta(days=1), write_scene("smoke", 8, 4, True)),          # smoke over the site only
    item("after_clear", D0 + timedelta(days=6), write_scene("aclear", 4, 4, True)),
    item("tiny_tile", D0 + timedelta(days=3), write_scene("tiny", 4, 4, True),
         geom=shp_transform(to_ll, box(sx - 100, sy - 100, sx + 100, sy + 100))),         # does not cover the AOI
    item("ctrl_before", DC - timedelta(days=10), write_scene("cb", 4, 4, False)),
    item("ctrl_after", DC + timedelta(days=4), write_scene("ca", 4, 4, False)),
]
D5 = date(2022, 6, 10)                                # only the older collection has scenes here
items += [
    item("old_before", D5 - timedelta(days=8), write_scene("ob", 4, 4, False), collection="sentinel-2-l2a", old_style=True),
    item("old_after", D5 + timedelta(days=2), write_scene("oa", 4, 4, True), collection="sentinel-2-l2a", old_style=True),
]
searched = []
def fake_search(aoi, start, end, collection="sentinel-2-c1-l2a"):
    searched.append(collection)
    return [it for it in items if start <= m.item_date(it) <= end and it["collection"] == collection]
m.search_items = fake_search

events = tmp / "events.csv"
cols = ["event_id", "site_id", "category", "place_name", "lat_site", "lon_site", "site_uncertainty_km", "event_time_utc"]
rows = [dict(event_id="E1", site_id="S1", category="large_outdoor_fire", place_name="Depot", lat_site=SITE[0], lon_site=SITE[1],
             site_uncertainty_km="0.4", event_time_utc="2025-01-07T23:00Z"),
        dict(event_id="E2", site_id="S1", category="negative_control", place_name="Depot", lat_site=SITE[0], lon_site=SITE[1],
             site_uncertainty_km="0.4", event_time_utc=DC.isoformat()),
        dict(event_id="E5", site_id="S1", category="large_outdoor_fire", place_name="Depot", lat_site=SITE[0], lon_site=SITE[1],
             site_uncertainty_km="0.4", event_time_utc=D5.isoformat()),
        dict(event_id="E3", site_id="S9", category="industrial_fire", place_name="No site", lat_site="", lon_site="",
             site_uncertainty_km="", event_time_utc="2025-01-07"),
        dict(event_id="E4", site_id="S8", category="port_ship_fire", place_name="Ship", lat_site="14", lon_site="41",
             site_uncertainty_km="20", event_time_utc="2025-01-07")]
with open(events, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
fp = tmp / "fp.geojson"
fp.write_text(json.dumps({"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"site_id": "S1"}, "geometry": mapping(footprint)}]}))
lab = tmp / "labelled.csv"
lab.write_text("event_id,window,latitude,longitude,signal\nE1,event,55.0,40.0,True\nE1,event,55.01,40.01,False\n", encoding="utf-8")

args = types.SimpleNamespace(events=events, footprints=fp, labelled=lab, only=None, offline=False)
m.cmd_run(args)
S = {r["event_id"]: r for r in csv.DictReader(open(m.OUT / "s2_summary.csv", encoding="utf-8"))}
e1 = S["E1"]
expect(e1["before_date"] == str(D0 - timedelta(days=12)), f"newest clear before-scene chosen, cloudy one skipped ({e1['before_date']})")
expect(e1["first_after_date"] == str(D0 + timedelta(days=1)) and float(e1["first_after_cloud"]) > 0.9,
       f"first-after scene shown even though smoke covers the site ({e1['first_after_date']}, {e1['first_after_cloud']})")
expect(e1["clear_after_date"] == str(D0 + timedelta(days=6)),
       f"smoke over the site alone makes a scene not clear, despite a clear ring ({e1['clear_after_date']})")
expect(e1["status"] == "ok", "E1 status ok")
expect(abs(float(e1["dnbr_site_median"]) - 0.75) < 0.05 and abs(float(e1["dnbr_ring_median"])) < 0.02,
       f"dNBR 0.75 inside the burnt site, ~0 in the ring despite water and offset ({e1['dnbr_site_median']}, {e1['dnbr_ring_median']})")
expect(all(abs(float(r[k])) <= 2 for r in csv.DictReader(open(m.OUT / "s2_summary.csv", encoding="utf-8"))
           for k in ("dnbr_site_median", "dnbr_ring_median", "dnbr_site_minus_ring") if r.get(k)),
       "no dNBR outside the possible range [-2, 2] (dark / water pixels no longer blow up)")
expect(300_000 < int(e1["area_site_dnbr_gt_0_27_m2"]) < 420_000, f"burnt area ~360,000 m2 ({e1['area_site_dnbr_gt_0_27_m2']})")
expect(float(e1["excess_frac_gt_0_27"]) > 0.9, "burnt share beyond the ring's")
expect(float(e1["dnbr_site_minus_ring"]) > 0.7 and float(e1["frac_site_dnbr_gt_0_27"]) > 0.9, "site minus ring and burnt fraction")
expect(int(e1["n_scenes_after"]) == 2, f"tile that does not cover the AOI is ignored ({e1['n_scenes_after']} after-scenes)")
e2 = S["E2"]
expect(e2["status"] == "ok" and abs(float(e2["dnbr_site_minus_ring"])) < 0.02, f"control: no change ({e2['dnbr_site_minus_ring']})")
e5 = S["E5"]
expect(e5["status"] == "ok" and e5["collections"] == "sentinel-2-l2a",
       f"2022 event: Collection 1 empty -> older collection used ({e5['status']}, {e5['collections']})")
expect(abs(float(e5["dnbr_site_median"]) - 0.75) < 0.05,
       f"older collection's +1000 offset removed from the item properties, so dNBR is right ({e5['dnbr_site_median']})")
expect(e1["collections"] == "sentinel-2-c1-l2a", "Collection 1 used where it has scenes")
expect(S["E3"]["status"] == "skipped_no_site_point" and S["E4"]["status"] == "skipped_position_too_vague", "skips")
expect((m.OUT / "panels" / "E1.png").exists() and (m.OUT / "panels" / "E1.png").stat().st_size > 20000, "full panel written")
bid = e1["blind_id"]
import re as _re
expect((m.OUT / "blind" / f"{bid}.png").exists() and _re.fullmatch(r"B[0-9A-F]{6}", bid) and bid != m.blind_id("E2"),
       f"blind panel written under a random-looking id, different per event ({bid})")
key = {r["blind_id"]: r["event_id"] for r in csv.DictReader(open(m.OUT / "blind_key.csv", encoding="utf-8"))}
expect(key.get(bid) == "E1" and len(key) == 3, "blind key maps every image (E1, E2, E5)")
expect(m.blind_id("E1") == bid, "blind id is stable between runs")

# a label entered by hand survives a rerun (offline, from the cache)
lines = (m.OUT / "blind_review.csv").read_text(encoding="utf-8").splitlines()
lines = [l.replace(f"{bid},blind/{bid}.png,,,", f"{bid},blind/{bid}.png,new_damage_or_scar,high,tanks gone") for l in lines]
(m.OUT / "blind_review.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
m.search_items = lambda *a, **k: fake_search(*a, **k)
import rasterio as _r
real_open = _r.open
m.rasterio.open = lambda *a, **k: (_ for _ in ()).throw(AssertionError("raster read during offline rerun"))
m.cmd_run(types.SimpleNamespace(**{**vars(args), "offline": True}))
m.rasterio.open = real_open
review = [l for l in (m.OUT / "blind_review.csv").read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
rv = {r["blind_id"]: r for r in csv.DictReader(review)}
expect(rv[bid]["label"] == "new_damage_or_scar" and rv[bid]["notes"] == "tanks gone", "hand-entered label kept after rerun")
S2 = {r["event_id"]: r for r in csv.DictReader(open(m.OUT / "s2_summary.csv", encoding="utf-8"))}
expect(S2["E1"]["dnbr_site_median"] == e1["dnbr_site_median"], "offline rerun from cached pixels gives the same numbers")
sys.exit(1 if expect.failed else 0)
