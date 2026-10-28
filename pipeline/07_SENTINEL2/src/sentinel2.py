# /// script
# requires-python = ">=3.10"
# dependencies = ["pystac-client", "rasterio", "numpy", "matplotlib", "shapely>=2", "pyproj", "requests"]
# ///
"""Sentinel-2 before/after images and a burn measure for every site-level event.

    uv run src/sentinel2.py run --only E017 E006 E021     # three events first
    uv run src/sentinel2.py run                           # all events with a site point
    uv run src/sentinel2.py run --offline                 # rebuild from the cache only

For each event (controls included) it searches Sentinel-2 L2A (Collection 1, Element 84 Earth
Search; public, no account) and picks three scenes:

  before        newest scene in the 45 days before the event that is clear over the site
  first after   first scene in the 5 days from the event day, cloudy or not (smoke, flames)
  clear after   first scene in the 30 days from the event day that is clear over the site

"Clear" is judged from the scene classification layer (SCL) over the site and its surroundings,
not from the scene-wide cloud percentage. Smoke is often classified as cloud, which is why the
first-after scene is shown regardless.

All scenes are warped onto one 10 m UTM grid, so before and after line up pixel for pixel.

Burn measure: dNBR = NBR(before) - NBR(clear after), NBR = (B8A - B12) / (B8A + B12), on pixels
clear in both. Reported inside the site outline and in a ring 0.5-1.5 km around it; the
difference (site - ring) removes changes that affect the whole area (season, light, haze).
dNBR was designed for vegetation; on tanks, roofs and asphalt it is a rough indicator only.

Outputs
  outputs/panels/<event>.png    full panel: true colour + SWIR, outline, FIRMS detections (05)
  outputs/blind/<blind id>.png  same images, no event id, no category, no FIRMS: for labelling
  outputs/blind_review.csv      one row per blind image: fill in label / confidence / notes
  outputs/blind_key.csv         blind id -> event: do not open until labelling is finished
  outputs/s2_summary.csv        scenes chosen, cloud over the site, dNBR numbers, status
"""
import argparse
import csv
import hashlib
import json
import math
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from shapely.geometry import Point, mapping, shape
from shapely.ops import transform as shp_transform

ROOT = Path(__file__).resolve().parents[1]          # 07_SENTINEL2
PROJECT = ROOT.parent
EVENTS = PROJECT / "02_OSM_CHECK" / "data" / "events_reviewed.csv"
FOOTPRINTS = PROJECT / "02_OSM_CHECK" / "data" / "site_footprints.geojson"
LABELLED = PROJECT / "05_PERSISTENT_HEAT" / "outputs" / "labelled_detections.csv"
CACHE = ROOT / "data" / "cache" / "s2"
OUT = ROOT / "outputs"

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-c1-l2a"
FALLBACK_COLLECTION = "sentinel-2-l2a"   # older processing; used where Collection 1 has no scenes (e.g. mid-2022)

BEFORE_DAYS = 45
FIRST_AFTER_DAYS = 5
CLEAR_AFTER_DAYS = 30
MAX_CLOUD = 0.20                 # max share of unusable SCL pixels, over the site and over the ring
MAX_CANDIDATES = 8               # scenes tried per side before giving up
RES_M = 10.0
AOI_MARGIN_KM = 2.0              # around the outline / point uncertainty
RING_KM = (0.5, 1.5)             # comparison ring around the site
SKIP_UNCERTAINTY_KM = 5.0        # ships at sea: no fixed site to image
POINT_SITE_MIN_KM = 0.3          # site radius when there is no outline

# SCL classes: 0 no data, 1 saturated, 3 cloud shadow, 8/9 cloud, 10 cirrus are unusable
SCL_BAD = {0, 1, 3, 8, 9, 10}
SCL_SNOW = 11                    # usable for looking, but excluded from dNBR (a thaw looks like a burn)
SCL_WATER = 6                    # excluded from dNBR: NBR is meaningless on water
DNBR_LOW, DNBR_MODERATE = 0.10, 0.27     # USGS burn-severity breakpoints (vegetation)

# Asset names differ between collections; first match wins.
ASSETS = {"B02": ["blue", "B02"], "B03": ["green", "B03"], "B04": ["red", "B04"],
          "B8A": ["nir08", "B8A"], "B11": ["swir16", "B11"], "B12": ["swir22", "B12"], "SCL": ["scl", "SCL"]}

LABELS = "active_fire | smoke | new_damage_or_scar | nothing_visible | not_assessable"


# ------------------------------------------------------------------ STAC (patched in tests)
def search_items(aoi, start, end, collection=COLLECTION):
    """All items intersecting aoi (shapely, WGS84) between two dates, cached as JSON."""
    key = hashlib.sha1(f"{collection}|{[round(v, 4) for v in aoi.bounds]}|{start}|{end}".encode()).hexdigest()[:16]
    path = CACHE / f"search_{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if OFFLINE:
        return None
    from pystac_client import Client
    client = Client.open(STAC_URL)
    s = client.search(collections=[collection], intersects=mapping(aoi),
                      datetime=f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z", max_items=300)
    items = [i.to_dict() for i in s.items()]
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items), encoding="utf-8")
    return items


OFFLINE = False


def search_with_fallback(aoi, start, end):
    """Collection 1 first; if it has no scene covering the AOI, the older collection."""
    items = search_items(aoi, start, end, COLLECTION)
    if items is None:
        return None
    if usable_items(items, aoi):
        return items
    older = search_items(aoi, start, end, FALLBACK_COLLECTION)
    return items if older is None else older


def asset_info(item, band):
    for name in ASSETS[band]:
        a = item.get("assets", {}).get(name)
        if a:
            href = a["href"]
            if href.startswith("s3://"):                 # public bucket, read over https
                bucket, key = href[5:].split("/", 1)
                href = f"https://{bucket}.s3.us-west-2.amazonaws.com/{key}"
            rb = (a.get("raster:bands") or [{}])[0]
            if band == "SCL":
                return href, 1, 0.0
            offset = rb.get("offset")
            if offset is None:
                # Older collection: from processing baseline 04.00 (Jan 2022) ESA adds +1000 to the
                # stored values; if the item says that offset was not removed, remove it here.
                props = item.get("properties", {})
                baseline = str(props.get("s2:processing_baseline", "0"))
                try:
                    late = float(baseline) >= 4.0
                except ValueError:
                    late = False
                offset = -0.1 if (late and props.get("earthsearch:boa_offset_applied") is False) else 0.0
            return href, rb.get("scale", 1e-4), offset
    return None, None, None


# ------------------------------------------------------------------ grid and reading
class TargetGrid:
    """10 m grid in the local UTM zone covering the AOI; every scene is warped onto it."""
    def __init__(self, aoi):
        lon, lat = aoi.centroid.x, aoi.centroid.y
        zone = int((lon + 180) // 6) + 1
        self.epsg = (32600 if lat >= 0 else 32700) + zone
        self.crs = CRS.from_epsg(self.epsg)
        self.to_utm = Transformer.from_crs("EPSG:4326", self.epsg, always_xy=True).transform
        u = shp_transform(self.to_utm, aoi)
        minx, miny, maxx, maxy = u.bounds
        minx, miny = math.floor(minx / RES_M) * RES_M, math.floor(miny / RES_M) * RES_M
        maxx, maxy = math.ceil(maxx / RES_M) * RES_M, math.ceil(maxy / RES_M) * RES_M
        self.bounds = (minx, miny, maxx, maxy)
        self.width, self.height = int((maxx - minx) / RES_M), int((maxy - miny) / RES_M)
        self.transform = from_origin(minx, maxy, RES_M, RES_M)

    def key(self):
        return f"{self.epsg}_{'_'.join(str(int(v)) for v in self.bounds)}"

    def mask(self, geom_wgs84):
        g = shp_transform(self.to_utm, geom_wgs84)
        if g.is_empty:
            return np.zeros((self.height, self.width), bool)
        return rasterize([(g, 1)], out_shape=(self.height, self.width), transform=self.transform, fill=0).astype(bool)


def read_band(item, band, grid):
    """Band on the target grid, as reflectance (float32) or SCL class (uint8). Cached as uint16/uint8."""
    href, scale, offset = asset_info(item, band)
    if href is None:
        return None
    path = CACHE / f"{item['id']}_{band}_{grid.key()}.npz"
    if path.exists():
        raw = np.load(path)["a"]
    elif OFFLINE:
        return None
    else:
        resampling = Resampling.nearest if band == "SCL" else Resampling.bilinear
        with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", AWS_NO_SIGN_REQUEST="YES",
                          CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.TIF"):
            with rasterio.open(href) as src:
                with WarpedVRT(src, crs=grid.crs, transform=grid.transform, width=grid.width,
                               height=grid.height, resampling=resampling, nodata=0) as vrt:
                    raw = vrt.read(1)
        CACHE.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, a=raw)
    if band == "SCL":
        return raw.astype(np.uint8)
    refl = raw.astype(np.float32) * float(scale) + float(offset)
    refl[raw == 0] = np.nan
    return refl


# ------------------------------------------------------------------ scene choice
def item_date(item):
    return datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00")).date()


def usable_items(items, aoi):
    """Items whose footprint contains the whole AOI; one per day."""
    out, seen = [], set()
    for it in sorted(items or [], key=lambda i: i["properties"]["datetime"]):
        try:
            if not shape(it["geometry"]).contains(aoi):
                continue
        except Exception:
            continue
        d = item_date(it)
        if d in seen:
            continue
        seen.add(d)
        out.append(it)
    return out


def cloud_share(item, grid, site_m, ring_m):
    """Unusable share (cloud, shadow, cirrus, no data) over the site and over the ring, separately.

    Measured separately on purpose: the ring is much larger than the site, so smoke or cloud
    sitting only on the site would be diluted to a few percent if both were pooled."""
    scl = read_band(item, "SCL", grid)
    if scl is None or not site_m.any():
        return None
    bad = np.isin(scl, list(SCL_BAD))
    ring = float(bad[ring_m].mean()) if ring_m.any() else 0.0
    return float(bad[site_m].mean()), ring


def pick(candidates, grid, site_m, ring_m, need_clear):
    """First candidate (in the given order) clear over BOTH site and ring; or the first readable one.
    Returns (item, site cloud share)."""
    for it in candidates[:MAX_CANDIDATES]:
        c = cloud_share(it, grid, site_m, ring_m)
        if c is None:
            continue
        if not need_clear or max(c) <= MAX_CLOUD:
            return it, round(c[0], 3)
    return None, None


# ------------------------------------------------------------------ measures and images
MIN_NBR_DENOM = 0.02             # B8A + B12 reflectance below this (water, deep shadow): NBR undefined


def nbr(item, grid):
    """NBR in [-1, 1]; NaN where it is not defined.

    Collection 1 reflectance carries an offset, so very dark pixels (water) can be ~0 or
    negative; dividing by their sum produced values in the hundreds. Those pixels are now NaN."""
    n, s = read_band(item, "B8A", grid), read_band(item, "B12", grid)
    if n is None or s is None:
        return None
    denom = n + s
    with np.errstate(invalid="ignore", divide="ignore"):
        v = (n - s) / denom
    v[~(denom > MIN_NBR_DENOM) | (n < 0) | (s < 0)] = np.nan
    return np.clip(v, -1.0, 1.0)


def dnbr_stats(before, after, grid, site_m, ring_m):
    """Median dNBR in the site and in the ring (medians: a few odd pixels cannot dominate),
    the share and area of site pixels above the burn breakpoints, and site minus ring."""
    nb, na = nbr(before, grid), nbr(after, grid)
    sb, sa = read_band(before, "SCL", grid), read_band(after, "SCL", grid)
    if nb is None or na is None or sb is None or sa is None:
        return {}
    bad = list(SCL_BAD | {SCL_SNOW, SCL_WATER})
    ok = ~np.isin(sb, bad) & ~np.isin(sa, bad) & np.isfinite(nb) & np.isfinite(na)
    d = nb - na
    s, r = d[site_m & ok], d[ring_m & ok]
    px_m2 = RES_M * RES_M
    out = {"n_valid_site_px": int(s.size), "n_valid_ring_px": int(r.size)}
    if s.size >= 5:
        out.update(dnbr_site_median=round(float(np.median(s)), 3),
                   frac_site_dnbr_gt_0_10=round(float((s > DNBR_LOW).mean()), 3),
                   frac_site_dnbr_gt_0_27=round(float((s > DNBR_MODERATE).mean()), 3),
                   area_site_dnbr_gt_0_27_m2=int((s > DNBR_MODERATE).sum() * px_m2))
    if r.size >= 5:
        out["dnbr_ring_median"] = round(float(np.median(r)), 3)
        out["frac_ring_dnbr_gt_0_27"] = round(float((r > DNBR_MODERATE).mean()), 3)
    if "dnbr_site_median" in out and "dnbr_ring_median" in out:
        out["dnbr_site_minus_ring"] = round(out["dnbr_site_median"] - out["dnbr_ring_median"], 3)
        # burnt share in the site beyond what the surroundings show anyway
        out["excess_frac_gt_0_27"] = round(out["frac_site_dnbr_gt_0_27"] - out["frac_ring_dnbr_gt_0_27"], 3)
    return out


def rgb(item, grid, bands, top):
    arrs = [read_band(item, b, grid) for b in bands]
    if any(a is None for a in arrs):
        return None
    img = np.dstack([np.clip(np.nan_to_num(a) / top, 0, 1) for a in arrs]) ** (1 / 1.4)
    return img


def draw_panel(path, grid, scenes, d0, site_geom, footprint, dets, title, blind):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = ["before", "first after", "clear after"]
    fig, axes = plt.subplots(2, 3, figsize=(13, 8.6), dpi=110)
    ext = [grid.bounds[0], grid.bounds[2], grid.bounds[1], grid.bounds[3]]
    outline = shp_transform(grid.to_utm, footprint if footprint is not None else site_geom)
    for col, (name, sc) in enumerate(zip(names, scenes)):
        item, cloud = sc
        for row, (bands, top, lab) in enumerate(((["B04", "B03", "B02"], 0.30, "true colour"),
                                                 (["B12", "B11", "B8A"], 0.60, "SWIR (B12-B11-B8A)"))):
            ax = axes[row][col]
            ax.set_xticks([]); ax.set_yticks([])
            img = rgb(item, grid, bands, top) if item else None
            if img is None:
                ax.set_facecolor("#222"); ax.text(0.5, 0.5, "no usable scene", color="w", ha="center", va="center",
                                                  transform=ax.transAxes)
            else:
                ax.imshow(img, extent=ext)
                for g in getattr(outline, "geoms", [outline]):
                    xs, ys = (g.exterior.xy if hasattr(g, "exterior") else g.xy)
                    ax.plot(xs, ys, color="#00e5ff", lw=1.2)
                if not blind:
                    for dt in dets:
                        x, y = grid.to_utm(float(dt["longitude"]), float(dt["latitude"]))
                        if dt.get("signal") == "True":
                            ax.plot(x, y, "o", mfc="none", mec="#ff2d2d", ms=7, mew=1.6)
                        else:
                            ax.plot(x, y, "x", color="#dddddd", ms=5, mew=1.0)
                ax.set_xlim(ext[0], ext[1]); ax.set_ylim(ext[2], ext[3])
            if row == 0:
                when = "" if not item else (f"{(item_date(item) - d0).days:+d} d" if blind else
                                            f"{item_date(item)} ({(item_date(item) - d0).days:+d} d)")
                cl = "" if cloud is None else f", cloud over site {cloud:.0%}"
                ax.set_title(f"{name}: {when}{cl}", fontsize=9)
            if col == 0:
                ax.set_ylabel(lab, fontsize=9)
    fig.suptitle(title, fontsize=11)
    note = "cyan: site outline" + ("" if blind else "   red circle: FIRMS signal   grey x: FIRMS background (05)")
    fig.text(0.01, 0.01, note + "   Contains modified Copernicus Sentinel data.", fontsize=7)
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


# ------------------------------------------------------------------ blind review bookkeeping
def blind_id(event_id):
    salt_path = ROOT / "data" / "blind_salt.txt"
    if not salt_path.exists():
        salt_path.parent.mkdir(parents=True, exist_ok=True)
        salt_path.write_text(secrets.token_hex(16), encoding="utf-8")
    salt = salt_path.read_text(encoding="utf-8").strip()
    return "B" + hashlib.sha256((salt + event_id).encode()).hexdigest()[:6].upper()


def update_blind_files(rows):
    """Add new blind ids; never overwrite labels already entered."""
    review = OUT / "blind_review.csv"
    existing = {}
    if review.exists():
        lines = [l for l in review.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
        existing = {r["blind_id"]: r for r in csv.DictReader(lines) if r.get("blind_id")}
    for r in rows:
        existing.setdefault(r["blind_id"], {"blind_id": r["blind_id"], "image": f"blind/{r['blind_id']}.png",
                                            "label": "", "confidence": "", "notes": ""})
    ordered = sorted(existing.values(), key=lambda r: r["blind_id"])
    with open(review, "w", newline="", encoding="utf-8") as f:
        f.write(f"# label: one of {LABELS}; confidence: high / medium / low\n")
        w = csv.DictWriter(f, fieldnames=["blind_id", "image", "label", "confidence", "notes"])
        w.writeheader(); w.writerows(ordered)
    keyf = OUT / "blind_key.csv"
    key = {}
    if keyf.exists():
        key = {r["blind_id"]: r["event_id"] for r in csv.DictReader(open(keyf, encoding="utf-8"))}
    key.update({r["blind_id"]: r["event_id"] for r in rows})
    with open(keyf, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["blind_id", "event_id"])
        for k in sorted(key):
            w.writerow([k, key[k]])


# ------------------------------------------------------------------ main
def to_float(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def buffer_km(geom, km):
    lat = geom.centroid.y
    # buffer in a local projection so the distance is the same in every direction
    crs = f"+proj=aeqd +lat_0={lat} +lon_0={geom.centroid.x} +datum=WGS84 +units=m"
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    return shp_transform(inv, shp_transform(fwd, geom).buffer(km * 1000))


def cmd_run(args):
    global OFFLINE
    OFFLINE = args.offline
    footprints = {}
    fp_path = Path(args.footprints)
    if fp_path.exists():
        for f in json.loads(fp_path.read_text(encoding="utf-8")).get("features", []):
            footprints[f["properties"]["site_id"]] = shape(f["geometry"])
    dets = {}
    if Path(args.labelled).exists():
        for r in csv.DictReader(open(args.labelled, encoding="utf-8")):
            dets.setdefault(r["event_id"], []).append(r)
    events = list(csv.DictReader(open(args.events, encoding="utf-8")))

    rows = []
    for ev in events:
        if args.only and ev["event_id"] not in args.only:
            continue
        base = {"event_id": ev["event_id"], "site_id": ev["site_id"], "category": ev["category"],
                "place_name": ev["place_name"], "event_time_utc": ev["event_time_utc"]}
        if not ev["lat_site"]:
            rows.append({**base, "status": "skipped_no_site_point"}); continue
        if (to_float(ev.get("site_uncertainty_km"), 0) or 0) > SKIP_UNCERTAINTY_KM:
            rows.append({**base, "status": "skipped_position_too_vague"}); continue
        print(f"{ev['event_id']} {ev['site_id']} {ev['place_name'][:50]} {ev['event_time_utc'][:10]}", flush=True)
        fp = footprints.get(ev["site_id"])
        if fp is None:
            lat, lon = float(ev["lat_site"]), float(ev["lon_site"])
            r_km = max(to_float(ev.get("site_uncertainty_km"), 0) or 0, POINT_SITE_MIN_KM)
            site = buffer_km(Point(lon, lat), r_km)
        else:
            site = fp
        ring = buffer_km(site, RING_KM[1]).difference(buffer_km(site, RING_KM[0]))
        aoi = buffer_km(site, max(AOI_MARGIN_KM, RING_KM[1] + 0.2)).envelope
        grid = TargetGrid(aoi)
        site_m, ring_m = grid.mask(site), grid.mask(ring)
        d0 = datetime.fromisoformat(ev["event_time_utc"].rstrip("Z")[:10]).date()

        before_items = search_with_fallback(aoi, d0 - timedelta(days=BEFORE_DAYS), d0 - timedelta(days=1))
        after_items = search_with_fallback(aoi, d0, d0 + timedelta(days=CLEAR_AFTER_DAYS))
        if before_items is None or after_items is None:
            rows.append({**base, "status": "not_cached"}); continue
        before_c = list(reversed(usable_items(before_items, aoi)))
        after_c = usable_items(after_items, aoi)
        before, bc = pick(before_c, grid, site_m, ring_m, need_clear=True)
        first, fc = pick([i for i in after_c if (item_date(i) - d0).days <= FIRST_AFTER_DAYS], grid, site_m, ring_m, need_clear=False)
        clear, cc = pick(after_c, grid, site_m, ring_m, need_clear=True)

        stats = dnbr_stats(before, clear, grid, site_m, ring_m) if before and clear else {}
        status = ("ok" if before and clear else
                  "no_clear_before" if not before and clear else
                  "no_clear_after" if before and not clear else
                  "no_clear_scenes" if (before_c or after_c) else "no_scenes")
        bid = blind_id(ev["event_id"])
        scenes = [(before, bc), (first, fc), (clear, cc)]
        my_dets = [d for d in dets.get(ev["event_id"], []) if d.get("window", "event") == "event"]
        if any(s[0] for s in scenes):
            draw_panel(OUT / "panels" / f"{ev['event_id']}.png", grid, scenes, d0, site, fp, my_dets,
                       f"{ev['event_id']} {ev['place_name']} - event {d0} ({ev['category']})", blind=False)
            draw_panel(OUT / "blind" / f"{bid}.png", grid, scenes, d0, site, fp, [], f"{bid}", blind=True)
        rows.append({**base, "status": status, "blind_id": bid,
                     "before_date": item_date(before).isoformat() if before else "", "before_cloud": bc if bc is not None else "",
                     "first_after_date": item_date(first).isoformat() if first else "",
                     "first_after_days": (item_date(first) - d0).days if first else "", "first_after_cloud": fc if fc is not None else "",
                     "clear_after_date": item_date(clear).isoformat() if clear else "",
                     "clear_after_days": (item_date(clear) - d0).days if clear else "", "clear_after_cloud": cc if cc is not None else "",
                     "n_scenes_before": len(before_c), "n_scenes_after": len(after_c),
                     **stats,
                     "before_id": before["id"] if before else "", "clear_after_id": clear["id"] if clear else "",
                     "collections": " ".join(sorted({it.get("collection", "?") for it in (before, first, clear) if it}))})
        print(f"    {status}: before {rows[-1]['before_date'] or '-'}, first after {rows[-1]['first_after_date'] or '-'}, "
              f"clear after {rows[-1]['clear_after_date'] or '-'}"
              + (f", dNBR site-ring {stats['dnbr_site_minus_ring']}" if "dnbr_site_minus_ring" in stats else ""), flush=True)

    OUT.mkdir(exist_ok=True)
    merged = merge_summary(OUT / "s2_summary.csv", rows)
    update_blind_files([r for r in rows if r.get("blind_id") and (OUT / "blind" / f"{r['blind_id']}.png").exists()])
    counts = {}
    for r in merged:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\nstatus:", counts)
    print(f"panels in {OUT / 'panels'}; label the images in {OUT / 'blind'} using blind_review.csv")


def merge_summary(path, rows):
    done = {r["event_id"] for r in rows}
    old = []
    if path.exists() and path.stat().st_size:
        old = [r for r in csv.DictReader(open(path, encoding="utf-8")) if r["event_id"] not in done]
    allr = old + rows
    cols = list(dict.fromkeys(k for r in allr for k in r))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval=""); w.writeheader(); w.writerows(allr)
    return allr


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--events", default=EVENTS)
    r.add_argument("--footprints", default=FOOTPRINTS)
    r.add_argument("--labelled", default=LABELLED)
    r.add_argument("--only", nargs="*")
    r.add_argument("--offline", action="store_true")
    args = p.parse_args()
    {"run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
