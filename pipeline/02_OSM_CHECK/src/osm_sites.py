# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "shapely>=2", "pyproj"]
# ///
"""Check and propose site coordinates against OpenStreetMap via Overpass.

    python src/osm_sites.py check     # query OSM, write candidates for review
    python src/osm_sites.py apply     # copy reviewed candidates into a new events file

Inputs
  data/events.csv       evaluation set (site_id, lat_site, lon_site, lat_coarse, ...)
  data/site_hints.csv   one row per site_id: how to search OSM for it

Outputs
  outputs/osm_candidates.csv      every candidate, ranked per site, with an empty accept column
  outputs/osm_candidates.geojson  same, with geometry, for QGIS
  outputs/osm_site_check.csv      one verdict per site
  data/cache/osm/*.json           raw Overpass responses (reruns cost no API calls)
"""
import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping
from shapely.ops import linemerge, polygonize_full, transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / "data" / "events.csv"
HINTS = ROOT / "data" / "site_hints.csv"
CACHE = ROOT / "data" / "cache" / "osm"
OUT = ROOT / "outputs"

ENDPOINT = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
# Overpass asks heavy users to identify themselves; put a contact in OVERPASS_UA if you can.
USER_AGENT = os.environ.get("OVERPASS_UA", "sv-corroboration-osm-check/0.1")
PAUSE_S = 2.0           # between live requests
# Exponential backoff when the server is busy: ~16 min worst case per site before giving up.
RETRY_WAITS_S = (15, 30, 60, 120, 240, 480)
BUSY_CODES = {429, 502, 503, 504}   # rate-limited / at capacity / gateway timeout: worth retrying

# Keys whose values are searched by name_regex.
NAME_KEY_LIST = ["name", "name:en", "name:ru", "name:uk", "official_name", "alt_name", "old_name", "short_name",
                 "operator", "brand"]
NAME_KEYS = "^(" + "|".join(NAME_KEY_LIST) + ")$"
# Elements carrying any of these keys are dropped (bus stops called "НПЗ", streets, fuel stations ...)
# unless the site's hint lists the key in allow_keys.
DROP_KEYS = {"highway", "public_transport", "route", "boundary", "place", "railway"}
# amenity is kept (hospitals, schools, restaurants are real targets) except these values:
DROP_AMENITY = {"fuel", "parking", "parking_space", "bus_station", "charging_station", "atm", "vending_machine"}

VERIFY_RADIUS_KM = 10.0  # search radius cap when the site already has coordinates
AGREE_KM = 0.5          # current point inside or within this of the feature
NEAR_KM = 2.0


# ---------------------------------------------------------------- geometry helpers
def local_projection(lat, lon):
    """Azimuthal equidistant projection centred on (lat, lon): metres, accurate near the centre."""
    crs = f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m"
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
    return fwd, inv


def element_geometry(el):
    """Overpass `out geom` element -> (shapely geometry in lon/lat, warning or "")."""
    t = el["type"]
    if t == "node":
        return Point(el["lon"], el["lat"]), ""
    if t == "way":
        raw = el.get("geometry", [])
        pts = [(p["lon"], p["lat"]) for p in raw if p]
        warn = "way has missing nodes" if len(pts) < len(raw) else ""
        if len(pts) >= 4 and pts[0] == pts[-1]:
            return Polygon(pts), warn
        geom = LineString(pts) if len(pts) >= 2 else (Point(pts[0]) if pts else None)
        return geom, warn
    if t == "relation":
        geom, warn = _relation_geometry(el)
        return geom, warn
    return None, "unknown element type"


def _rings(lines):
    """Close member ways into polygons; also report pieces that did not close."""
    if not lines:
        return [], False
    merged = linemerge(lines)
    parts = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    polys, cuts, dangles, invalid = polygonize_full(parts)
    left_open = not (cuts.is_empty and dangles.is_empty and invalid.is_empty)
    return list(polys.geoms), left_open


def _relation_geometry(el):
    is_area = el.get("tags", {}).get("type") in ("multipolygon", "boundary")
    outer, inner, other = [], [], []
    for m in el.get("members", []):
        raw = m.get("geometry") or []
        g = [(p["lon"], p["lat"]) for p in raw if p]
        if m["type"] == "way" and len(g) >= 2:
            role = m.get("role", "")
            if is_area and role in ("outer", ""):
                outer.append(LineString(g))
            elif is_area and role == "inner":
                inner.append(LineString(g))
            else:   # e.g. type=site relations grouping buildings: keep closed members as areas
                closed = len(g) >= 4 and g[0] == g[-1]
                other.append(Polygon(g) if closed else LineString(g))
        elif m["type"] == "node" and "lat" in m:
            other.append(Point(m["lon"], m["lat"]))
    if outer:
        shells, open_outer = _rings(outer)
        holes, _ = _rings(inner)
        if shells:
            area = unary_union(shells)
            if holes:
                area = area.difference(unary_union(holes))
            return area, ("outline partly open - area may be incomplete" if open_outer else "")
        # Outer ways exist but never close: do not pretend this is an area.
        return unary_union(outer + inner), "outline does not close - treated as lines, containment not tested"
    parts = inner + other
    if parts:
        return unary_union(parts), ("not an area relation" if not is_area else "")
    b = el.get("bounds")
    if b:   # last resort: bounding box
        box = Polygon([(b["minlon"], b["minlat"]), (b["maxlon"], b["minlat"]),
                       (b["maxlon"], b["maxlat"]), (b["minlon"], b["maxlat"])])
        return box, "no member geometry - bounding box used"
    return None, "no geometry"


def describe(geom_ll, ref_lat, ref_lon, site_lat=None, site_lon=None):
    """Distances (km) and size of a lon/lat geometry, measured in a local projection."""
    fwd, inv = local_projection(ref_lat, ref_lon)
    g = transform(fwd, geom_ll)
    rep = g.representative_point()          # guaranteed inside polygons, unlike the centroid
    rep_lon, rep_lat = inv(rep.x, rep.y)
    out = {
        "rep_lat": round(rep_lat, 6), "rep_lon": round(rep_lon, 6),
        "is_area": isinstance(g, (Polygon, MultiPolygon)),
        "area_km2": round(g.area / 1e6, 4),
        # half-size of the feature as seen from the representative point
        "extent_km": round(max(rep.hausdorff_distance(g), 0.0) / 1000, 3),
        "dist_to_coarse_km": round(g.distance(Point(0, 0)) / 1000, 2),
        "dist_to_site_km": "", "contains_site": "",
    }
    if site_lat not in (None, ""):
        sx, sy = fwd(float(site_lon), float(site_lat))
        sp = Point(sx, sy)
        out["dist_to_site_km"] = round(g.distance(sp) / 1000, 3)
        out["contains_site"] = bool(out["is_area"] and g.contains(sp))
    return out


# ---------------------------------------------------------------- Overpass
def ql_string(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def effective_hint(site, hint):
    """Copy of the hint with the radius actually used for this site.

    The hint radius is sized to reach from the coarse point to the site. When a site point
    already exists the search is centred on it, so a smaller circle is enough to verify it.
    """
    hint = dict(hint)
    if site["lat_site"] and hint.get("radius_km"):
        hint["radius_km"] = str(min(float(hint["radius_km"]), VERIFY_RADIUS_KM))
    return hint


def search_bbox(lat, lon, radius_km):
    """(south, west, north, east) square around the point; exact distance is filtered in Python."""
    lat, lon, r = float(lat), float(lon), float(radius_km)
    dlat = r / 111.32
    dlon = r / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return round(lat - dlat, 5), round(lon - dlon, 5), round(lat + dlat, 5), round(lon + dlon, 5)


def build_query(hint, lat, lon):
    """Overpass QL for one site.

    Speed matters on a busy server. Each statement filters on one fixed key (name, operator ...),
    which Overpass can look up in its tag index, inside a global bounding box. The first version
    matched keys by regex inside an `around` circle, which forces a scan of every tag of every
    object in the area - far too slow around a city like Volgograd.
    """
    s, w, n, e = search_bbox(lat, lon, hint["radius_km"])
    parts = []
    if hint["name_regex"]:
        rx = ql_string(hint["name_regex"])
        parts += [f'nwr["{k}"~"{rx}",i];' for k in NAME_KEY_LIST]
    if hint["tags"]:
        parts.append(f"nwr{hint['tags']};")
    if hint["mode"] == "address" and hint["street_regex"]:
        house = f'["addr:housenumber"="{ql_string(hint["housenumber"])}"]' if hint["housenumber"] else ""
        parts.append(f'nwr["addr:street"~"{ql_string(hint["street_regex"])}",i]{house};')
    if not parts:
        return None
    # Overpass admits queries partly by the resources they declare; asking for less than the
    # defaults (180 s, 512 MB) gets a slot sooner on a busy server. 64 MB is ample for one site.
    head = f"[out:json][timeout:60][maxsize:67108864][bbox:{s},{w},{n},{e}];"
    # `tags` verbosity drops relation members; complete_relations() fetches those separately.
    # Kept as is so existing cached responses stay valid.
    return head + "\n(\n  " + "\n  ".join(parts) + "\n);\nout tags geom;"


def server_status():
    """Print the server's slot status (Overpass /api/status) so you can see if it is worth running now."""
    url = ENDPOINT.rsplit("/", 1)[0] + "/status"
    try:
        text = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(10, 20)).text
    except requests.RequestException as exc:
        print(f"server status: no answer from {url} ({type(exc).__name__}) - server is probably overloaded")
        return
    lines = [l for l in text.splitlines() if "slot" in l.lower() or "rate limit" in l.lower()]
    print("server status: " + (" | ".join(lines[:4]) if lines else text.strip()[:200]))


def complete_relations(site_id, data, offline=False):
    """Fill in member geometry for relations that came back without members.

    `out tags geom` returns ways with their geometry, but at the `tags` verbosity relations
    lose their member list, so multi-part areas arrive as tags plus a bounding box only.
    This asks for just those relations again with `out body geom` (members included) in one
    small query, cached like any other, and merges the members in.
    Returns (data, ids that are still missing members).
    """
    need = [e["id"] for e in data.get("elements", []) if e["type"] == "relation" and not e.get("members")]
    if not need:
        return data, []
    ids = ",".join(str(i) for i in sorted(need))
    query = f"[out:json][timeout:60][maxsize:67108864];\nrelation(id:{ids});\nout body geom;"
    extra, _ = fetch(f"{site_id}_rel", query, offline=offline)
    full = {e["id"]: e for e in (extra or {}).get("elements", []) if e["type"] == "relation"}
    for e in data["elements"]:
        if e["type"] == "relation" and e["id"] in full and full[e["id"]].get("members"):
            e["members"] = full[e["id"]]["members"]
            e.setdefault("bounds", full[e["id"]].get("bounds"))
    still = [i for i in need if not full.get(i, {}).get("members")]
    return data, still


def fetch(site_id, query, offline=False):
    """Return Overpass JSON, from cache when possible."""
    key = hashlib.sha1(query.encode()).hexdigest()[:12]
    path = CACHE / f"{site_id}_{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), "cache"
    if offline:
        return None, "not_cached"
    retry_after = 0
    for wait in (0,) + RETRY_WAITS_S:
        wait = max(wait, retry_after)
        if wait:
            wait += random.uniform(0, wait * 0.2)   # jitter, so retries don't line up with everyone else's
            print(f"    waiting {wait:.0f}s before retry", flush=True)
            time.sleep(wait)
        retry_after = 0
        try:
            resp = requests.post(ENDPOINT, data={"data": query},
                                 headers={"User-Agent": USER_AGENT}, timeout=(15, 100))
        except requests.RequestException as exc:
            print(f"    no response ({type(exc).__name__}); will retry", flush=True)
            continue
        if resp.status_code == 200:
            data = resp.json()
            remark = data.get("remark", "")
            if "runtime error" in remark or "timed out" in remark:
                # Overpass can return 200 with a partial result and an error remark.
                print(f"    overpass remark: {remark[:120]}", flush=True)
                continue
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
            time.sleep(PAUSE_S)
            return data, "live"
        if resp.status_code in BUSY_CODES:
            print(f"    HTTP {resp.status_code}: server busy", flush=True)
            header = resp.headers.get("Retry-After", "")
            retry_after = int(header) if header.isdigit() else 0
            continue
        print(f"    HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
        return None, f"http_{resp.status_code}"
    return None, "gave_up"


# ---------------------------------------------------------------- candidates
def keep(el, hint):
    tags = el.get("tags", {})
    allowed = {k.strip() for k in hint["allow_keys"].split(",") if k.strip()}
    for k in DROP_KEYS - allowed:
        if k in tags:
            return False
    if "amenity" not in allowed and tags.get("amenity") in DROP_AMENITY:
        return False
    if tags.get("type") in ("route", "route_master", "boundary"):
        return False
    if hint["areas_only"] == "y" and el["type"] == "node":
        return False
    return True


def site_table(events_path):
    """First row per site_id carries the site-level columns."""
    sites = {}
    with open(events_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sites.setdefault(row["site_id"], row)
    return sites


def candidates_for(site, hint, data):
    name_re = re.compile(hint["name_regex"], re.I) if hint["name_regex"] else None
    street_re = re.compile(hint["street_regex"], re.I) if hint["mode"] == "address" and hint["street_regex"] else None
    rows = []
    seen = set()
    for el in data.get("elements", []):
        k = (el["type"], el["id"])
        if k in seen or not keep(el, hint):
            continue
        seen.add(k)
        geom, warn = element_geometry(el)
        if geom is None or geom.is_empty:
            continue
        tags = el.get("tags", {})
        names = {t: v for t, v in tags.items() if re.match(NAME_KEYS, t)}
        d = describe(geom, float(site["lat_coarse"]), float(site["lon_coarse"]),
                     site["lat_site"] or None, site["lon_site"] or None)
        # The query used a square box; keep only what lies inside the search circle.
        centre_km = d["dist_to_site_km"] if d["dist_to_site_km"] != "" else d["dist_to_coarse_km"]
        if centre_km > float(hint["radius_km"]):
            continue
        addr_match = bool(
            street_re and street_re.search(tags.get("addr:street", ""))
            and (not hint["housenumber"] or tags.get("addr:housenumber", "") == hint["housenumber"]))
        rows.append({
            "site_id": site["site_id"], "place_name": site["place_name"],
            "osm": f"{el['type']}/{el['id']}",
            "osm_url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
            "osm_name": tags.get("name", ""), "osm_name_en": tags.get("name:en", ""),
            "name_match": bool(name_re and any(name_re.search(v) for v in names.values())),
            "addr_match": addr_match,
            "geom_warning": warn,
            "key_tags": ";".join(f"{t}={tags[t]}" for t in ("landuse", "industrial", "man_made", "product", "power",
                                                            "aeroway", "military", "shop", "building", "amenity",
                                                            "addr:street", "addr:housenumber") if t in tags),
            **d, "accept": "", "_geom": geom,
        })
    # Rank: name match first, then real areas, then closest to the current site point
    # (or to the coarse point when there is no site point yet).
    def dist(r):
        return r["dist_to_site_km"] if r["dist_to_site_km"] != "" else r["dist_to_coarse_km"]
    rows.sort(key=lambda r: (not (r["name_match"] or r["addr_match"]), not r["is_area"], dist(r)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def verdict(site, cands):
    """Return (verdict, note, deciding candidate or None).

    Only candidates that matched by name or address can confirm a site. If none did, the
    verdict is computed from tag-only candidates and gets a `_tag_only` suffix, because an
    unnamed industrial polygon around the point is weak evidence that it is the right site.
    """
    if not cands:
        return "no_candidates", "", None
    notes = []
    matched = [c for c in cands if c["name_match"] or c["addr_match"]]
    if len(matched) >= 2:
        fwd, _ = local_projection(float(site["lat_coarse"]), float(site["lon_coarse"]))
        a = transform(fwd, matched[0]["_geom"]); b = transform(fwd, matched[1]["_geom"])
        if a.distance(b) > NEAR_KM * 1000:
            notes.append(f"ambiguous: {len(matched)} matches, top two {a.distance(b)/1000:.1f} km apart")
    pool, suffix = (matched, "") if matched else (cands, "_tag_only")
    if site["lat_site"] == "":
        deciding = pool[0]
        v = "proposal_only" + suffix
    else:
        inside = [c for c in pool if c["contains_site"] is True]
        deciding = inside[0] if inside else min(pool, key=lambda c: c["dist_to_site_km"])
        d = 0.0 if inside else deciding["dist_to_site_km"]
        v = ("agrees" if d <= AGREE_KM else "near" if d <= NEAR_KM else "disagrees") + suffix
    if deciding["geom_warning"]:
        notes.append(f"{deciding['osm']}: {deciding['geom_warning']}")
    return v, "; ".join(notes), deciding


def cmd_check(args):
    sites = site_table(args.events)
    with open(args.hints, newline="", encoding="utf-8") as f:
        hints = {r["site_id"]: r for r in csv.DictReader(f)}
    missing = sorted(set(sites) - set(hints))
    if missing:
        sys.exit(f"no hint rows for: {', '.join(missing)}")

    if not args.offline:
        server_status()
    all_cands, checks = [], []
    for sid in sorted(sites):
        if args.only and sid not in args.only:
            continue
        site = sites[sid]
        hint = effective_hint(site, hints[sid])
        base = {"site_id": sid, "place_name": site["place_name"], "had_site_coords": site["lat_site"] != "",
                "mode": hint["mode"], "hint_note": hint["note"]}
        if hint["mode"].startswith("skip"):
            checks.append({**base, "verdict": hint["mode"], "n_candidates": 0, "best_osm": "", "deciding_osm": "",
                           "note": "", "source": ""})
            continue
        # Search around the site point if there is one, otherwise around the coarse point.
        lat = site["lat_site"] or site["lat_coarse"]
        lon = site["lon_site"] or site["lon_coarse"]
        query = build_query(hint, lat, lon)
        print(f"{sid} {site['place_name'][:60]}", flush=True)
        data, src = fetch(sid, query, offline=args.offline)
        if data is None:
            checks.append({**base, "verdict": f"query_failed:{src}", "n_candidates": 0, "best_osm": "",
                           "deciding_osm": "", "note": "", "source": src})
            continue
        data, missing = complete_relations(sid, data, offline=args.offline)
        if missing:
            print(f"    {len(missing)} relation(s) still without members - bounding box used", flush=True)
        cands = candidates_for(site, hint, data)
        # Verdict on the full list; truncate only the review file, never dropping the deciding candidate.
        v, note, deciding = verdict(site, cands)
        shown = cands[: args.max_candidates]
        if deciding is not None and deciding not in shown:
            shown.append(deciding)
        checks.append({**base, "verdict": v, "n_candidates": len(cands), "n_shown": len(shown),
                       "best_osm": cands[0]["osm"] if cands else "",
                       "deciding_osm": deciding["osm"] if deciding else "",
                       "deciding_name": deciding["osm_name"] if deciding else "",
                       "deciding_dist_to_site_km": deciding["dist_to_site_km"] if deciding else "",
                       "note": note, "source": src})
        all_cands.extend(shown)
        print(f"    {v}: {len(cands)} candidates" + (f" ({note})" if note else ""), flush=True)

    OUT.mkdir(exist_ok=True)
    cols = ["site_id", "rank", "accept", "place_name", "osm", "osm_name", "osm_name_en", "name_match", "addr_match",
            "key_tags", "is_area", "area_km2", "extent_km", "rep_lat", "rep_lon", "dist_to_coarse_km",
            "dist_to_site_km", "contains_site", "geom_warning", "osm_url"]
    with open(OUT / "osm_candidates.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(all_cands)
    feats = [{"type": "Feature", "geometry": mapping(c["_geom"]),
              "properties": {k: c[k] for k in cols}} for c in all_cands]
    (OUT / "osm_candidates.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}),
                                                encoding="utf-8")
    ccols = ["site_id", "place_name", "mode", "had_site_coords", "verdict", "n_candidates", "n_shown", "best_osm",
             "deciding_osm", "deciding_name", "deciding_dist_to_site_km", "note", "hint_note", "source"]
    with open(OUT / "osm_site_check.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ccols, restval=""); w.writeheader(); w.writerows(checks)
    summary = {}
    for c in checks:
        summary[c["verdict"].split(":")[0]] = summary.get(c["verdict"].split(":")[0], 0) + 1
    print("\nverdicts:", summary)
    print(f"wrote {OUT/'osm_candidates.csv'}, .geojson and osm_site_check.csv")


def read_accept_marks(path):
    """(site_id, osm) pairs marked y in the candidates CSV.

    Tolerates the CSV having been re-saved by Excel or Numbers: a byte-order mark, `;` as the
    separator, or a non-UTF-8 encoding. Only site_id, osm and accept are read from it - names,
    coordinates and geometry come from the GeoJSON, which nobody edits by hand.
    """
    if not path.exists():
        return []
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "mac_roman"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    except (csv.Error, IndexError):
        dialect = csv.excel
    rows = list(csv.DictReader(text.splitlines(), dialect=dialect))
    if rows and not {"site_id", "osm", "accept"} <= set(rows[0]):
        sys.exit(f"{path.name}: columns site_id / osm / accept not found - was the header changed?")
    return [(r["site_id"].strip(), r["osm"].strip()) for r in rows
            if (r.get("accept") or "").strip().lower() in ("y", "yes", "1", "x")]


def cmd_apply(args):
    """Copy accepted candidates into data/events_reviewed.csv; never touches events.csv.

    Accept candidates either on the command line (--accept S14=way/121815231) or with y in the
    accept column of outputs/osm_candidates.csv. Both are combined.
    """
    geo_path = OUT / "osm_candidates.geojson"
    if not geo_path.exists():
        sys.exit("outputs/osm_candidates.geojson not found - run `check` first")
    geo = json.loads(geo_path.read_text(encoding="utf-8"))
    geoms = {(f["properties"]["site_id"], f["properties"]["osm"]): f for f in geo["features"]}

    marks = read_accept_marks(OUT / "osm_candidates.csv")
    for item in args.accept or []:
        if "=" not in item:
            sys.exit(f"--accept {item}: use SITE=OSM, e.g. S14=way/121815231")
        sid, osm = (s.strip() for s in item.split("=", 1))
        marks.append((sid, osm))

    by_site = {}
    for sid, osm in marks:
        if (sid, osm) not in geoms:
            options = [o for (s, o) in geoms if s == sid]
            sys.exit(f"{sid}={osm} is not a candidate from the last check. "
                     f"Candidates for {sid}: {', '.join(options) or 'none'}")
        if sid in by_site and by_site[sid]["osm"] != osm:
            sys.exit(f"{sid}: two different candidates accepted ({by_site[sid]['osm']}, {osm}) - keep one")
        by_site[sid] = geoms[(sid, osm)]["properties"]
    if not by_site:
        sys.exit("nothing accepted - pass --accept SITE=OSM or put y in the accept column")
    for sid, c in sorted(by_site.items()):
        print(f"accepting {sid}: {c['osm']} {c['osm_name']!r} ({c['rep_lat']}, {c['rep_lon']})")

    today = date.today().isoformat()
    with open(args.events, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames)
        rows = list(reader)
    for extra in ("osm_feature", "site_coord_previous"):
        if extra not in cols:
            cols.append(extra)
    changed = set()
    for row in rows:
        c = by_site.get(row["site_id"])
        if not c:
            continue
        if row["lat_site"]:
            row["site_coord_previous"] = f'{row["lat_site"]},{row["lon_site"]} ({row["site_coord_basis"]})'
        row["lat_site"], row["lon_site"] = c["rep_lat"], c["rep_lon"]
        row["site_coord_basis"] = f'OSM {c["osm"]} "{c["osm_name"]}" (reviewed {today})'
        row["site_uncertainty_km"] = max(float(c["extent_km"]), 0.1)
        row["osm_feature"] = c["osm"]
        if c["osm_url"] not in row["sources"]:
            row["sources"] = row["sources"] + " | " + c["osm_url"]
        changed.add(row["site_id"])
    out = ROOT / "data" / "events_reviewed.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    fp = {"type": "FeatureCollection",
          "features": [geoms[(s, c["osm"])] for s, c in by_site.items() if (s, c["osm"]) in geoms]}
    (ROOT / "data" / "site_footprints.geojson").write_text(json.dumps(fp), encoding="utf-8")
    print(f"updated {len(changed)} sites -> {out}; footprints -> data/site_footprints.geojson")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--events", default=EVENTS)
    c.add_argument("--hints", default=HINTS)
    c.add_argument("--only", nargs="*", help="site_ids to run, e.g. S14 S02")
    c.add_argument("--offline", action="store_true", help="use cached responses only")
    c.add_argument("--max-candidates", type=int, default=15)
    a = sub.add_parser("apply")
    a.add_argument("--events", default=EVENTS)
    a.add_argument("--accept", nargs="*", metavar="SITE=OSM",
                   help="accept candidates without editing the CSV, e.g. --accept S14=way/121815231")
    args = p.parse_args()
    {"check": cmd_check, "apply": cmd_apply}[args.cmd](args)


if __name__ == "__main__":
    main()
