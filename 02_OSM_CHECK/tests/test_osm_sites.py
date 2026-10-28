"""Offline test of osm_sites.py with hand-made Overpass responses (no network).

    python tests/test_osm_sites.py
"""
import csv, json, sys, tempfile, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import osm_sites as m

def box(lat, lon, d):            # closed way around (lat, lon), d degrees half-size
    return [{"lat": lat - d, "lon": lon - d}, {"lat": lat - d, "lon": lon + d},
            {"lat": lat + d, "lon": lon + d}, {"lat": lat + d, "lon": lon - d}, {"lat": lat - d, "lon": lon - d}]

def ring_members(lat, lon, d, role):  # same box split into two ways, as OSM relations often are
    b = box(lat, lon, d)
    return [{"type": "way", "ref": 1, "role": role, "geometry": b[:3]},
            {"type": "way", "ref": 2, "role": role, "geometry": b[2:]}]

tmp = Path(tempfile.mkdtemp())
m.ROOT, m.CACHE, m.OUT = tmp, tmp / "cache", tmp / "out"
(tmp / "data").mkdir()

events = tmp / "data" / "events.csv"
cols = ["event_id", "site_id", "category", "place_name", "lat_site", "lon_site", "site_coord_basis",
        "site_uncertainty_km", "lat_coarse", "lon_coarse", "sources", "notes"]
rows = [
    # A: has coordinates, OSM polygon contains them -> agrees
    dict(event_id="E1", site_id="A", category="x", place_name="Kristall depot", lat_site="51.43685", lon_site="46.09028",
         site_coord_basis="press", site_uncertainty_km="0.5", lat_coarse="51.48389", lon_coarse="46.10528", sources="s", notes=""),
    dict(event_id="E2", site_id="A", category="ctrl", place_name="Kristall depot", lat_site="51.43685", lon_site="46.09028",
         site_coord_basis="press", site_uncertainty_km="0.5", lat_coarse="51.48389", lon_coarse="46.10528", sources="s", notes=""),
    # B: no coordinates -> proposal_only, two named candidates far apart -> ambiguous
    dict(event_id="E3", site_id="B", category="x", place_name="Feodosia terminal", lat_site="", lon_site="",
         site_coord_basis="needs_geolocation", site_uncertainty_km="", lat_coarse="45.03199", lon_coarse="35.38153", sources="s", notes=""),
    # C: has coordinates but the only OSM feature is 5 km away -> disagrees
    dict(event_id="E4", site_id="C", category="x", place_name="Refinery", lat_site="48.48917", lon_site="44.62722",
         site_coord_basis="wiki", site_uncertainty_km="1.5", lat_coarse="48.71378", lon_coarse="44.4976", sources="s", notes=""),
    # D: skipped (offshore)
    dict(event_id="E5", site_id="D", category="x", place_name="Ship", lat_site="53.7", lon_site="0.39",
         site_coord_basis="wiki", site_uncertainty_km="1", lat_coarse="53.74", lon_coarse="-0.33", sources="s", notes=""),
    # F: unnamed tag-only polygon contains the point, a NAMED feature sits 5 km away -> named one decides: disagrees
    dict(event_id="E6", site_id="F", category="x", place_name="Depot", lat_site="50.0", lon_site="40.0",
         site_coord_basis="wiki", site_uncertainty_km="1", lat_coarse="50.1", lon_coarse="40.1", sources="s", notes=""),
    # G: only a tag-only polygon, containing the point -> agrees_tag_only
    dict(event_id="E7", site_id="G", category="x", place_name="Depot2", lat_site="51.0", lon_site="41.0",
         site_coord_basis="wiki", site_uncertainty_km="1", lat_coarse="51.1", lon_coarse="41.1", sources="s", notes=""),
    # K: deciding candidate (closest, a named line) is ranked below a named area; with max_candidates=1 it must survive
    #    the cut. Also includes a relation whose outer ways never close.
    dict(event_id="E8", site_id="K", category="x", place_name="Plant", lat_site="52.0", lon_site="42.0",
         site_coord_basis="wiki", site_uncertainty_km="1", lat_coarse="52.1", lon_coarse="42.1", sources="s", notes=""),
    # N: relation returned the way real `out tags geom` does it - tags + bounds, no members
    dict(event_id="E10", site_id="N", category="x", place_name="Refinery N", lat_site="53.0", lon_site="43.0",
         site_coord_basis="wiki", site_uncertainty_km="1", lat_coarse="53.05", lon_coarse="43.05", sources="s", notes=""),
    # M: address search
    dict(event_id="E9", site_id="M", category="x", place_name="House", lat_site="48.85", lon_site="2.34",
         site_coord_basis="wiki", site_uncertainty_km="0.2", lat_coarse="48.853", lon_coarse="2.349", sources="s", notes=""),
]
with open(events, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)

hints = tmp / "data" / "hints.csv"
hcols = ["site_id", "mode", "name_regex", "tags", "street_regex", "housenumber", "radius_km", "areas_only", "allow_keys", "note"]
H = [dict(site_id="A", mode="feature", name_regex="[Кк]ристалл|[Нн]ефтебаза", tags='["industrial"~"oil"]', street_regex="",
          housenumber="", radius_km="15", areas_only="y", allow_keys="", note=""),
     dict(site_id="B", mode="feature", name_regex="[Нн]ефтебаза", tags="", street_regex="", housenumber="", radius_km="10",
          areas_only="y", allow_keys="", note=""),
     dict(site_id="C", mode="feature", name_regex="НПЗ", tags="", street_regex="", housenumber="", radius_km="35",
          areas_only="n", allow_keys="", note=""),
     dict(site_id="D", mode="skip_offshore", name_regex="", tags="", street_regex="", housenumber="", radius_km="",
          areas_only="", allow_keys="", note=""),
     dict(site_id="F", mode="feature", name_regex="[Нн]ефтебаза", tags='["industrial"~"oil"]', street_regex="",
          housenumber="", radius_km="10", areas_only="y", allow_keys="", note=""),
     dict(site_id="G", mode="feature", name_regex="[Нн]ефтебаза", tags='["industrial"~"oil"]', street_regex="",
          housenumber="", radius_km="10", areas_only="y", allow_keys="", note=""),
     dict(site_id="K", mode="feature", name_regex="Plant", tags="", street_regex="", housenumber="", radius_km="10",
          areas_only="n", allow_keys="", note=""),
     dict(site_id="N", mode="feature", name_regex="Refinery", tags="", street_regex="", housenumber="", radius_km="10",
          areas_only="y", allow_keys="", note=""),
     dict(site_id="M", mode="address", name_regex="", tags="", street_regex="[Rr]ue Saint-Jacques", housenumber="277",
          radius_km="5", areas_only="n", allow_keys="", note="")]
with open(hints, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=hcols); w.writeheader(); w.writerows(H)

fixtures = {
    "A": [
        {"type": "node", "id": 10, "lat": 51.44, "lon": 46.10, "tags": {"name": "Нефтебаза", "highway": "bus_stop"}},  # dropped
        {"type": "node", "id": 11, "lat": 51.45, "lon": 46.11, "tags": {"name": "Кристалл", "shop": "convenience"}},  # dropped (node, areas_only)
        {"type": "node", "id": 12, "lat": 51.45, "lon": 46.12, "tags": {"name": "Нефтебаза АЗС", "amenity": "fuel"}},  # dropped
        {"type": "way", "id": 20, "tags": {"name": "Комбинат «Кристалл»", "landuse": "industrial"},
         "geometry": box(51.43685, 46.09028, 0.004)},                                                             # contains site
        {"type": "relation", "id": 30, "tags": {"type": "multipolygon", "name": "Нефтебаза №2", "landuse": "industrial"},
         "members": ring_members(51.48, 46.15, 0.003, "outer")},
        # inside the square query box but ~13 km away, outside the (capped) 10 km circle -> dropped
        {"type": "way", "id": 21, "tags": {"name": "Нефтебаза угол", "landuse": "industrial"},
         "geometry": box(51.43685 + 0.085, 46.09028 + 0.14, 0.001)},
    ],
    "B": [
        {"type": "way", "id": 40, "tags": {"name": "Нефтебаза", "landuse": "industrial"}, "geometry": box(45.05, 35.40, 0.003)},
        {"type": "way", "id": 41, "tags": {"name": "Феодосийская нефтебаза", "landuse": "industrial"}, "geometry": box(45.00, 35.30, 0.003)},
    ],
    "C": [
        {"type": "way", "id": 50, "tags": {"name": "НПЗ", "landuse": "industrial"}, "geometry": box(48.535, 44.627, 0.003)},
        {"type": "way", "id": 51, "tags": {"name": "улица НПЗ", "highway": "residential"},
         "geometry": [{"lat": 48.489, "lon": 44.62}, {"lat": 48.49, "lon": 44.63}]},                              # dropped
    ],
    "F": [
        {"type": "way", "id": 60, "tags": {"landuse": "industrial", "industrial": "oil"}, "geometry": box(50.0, 40.0, 0.003)},
        {"type": "way", "id": 61, "tags": {"name": "Нефтебаза", "landuse": "industrial"}, "geometry": box(50.045, 40.0, 0.003)},
    ],
    "G": [
        {"type": "way", "id": 70, "tags": {"landuse": "industrial", "industrial": "oil"}, "geometry": box(51.0, 41.0, 0.003)},
    ],
    "K": [
        # named area 3 km away: ranked first (area beats line)
        {"type": "way", "id": 80, "tags": {"name": "Plant North", "landuse": "industrial"}, "geometry": box(52.027, 42.0, 0.002)},
        # named open way passing 0.1 km from the point: closest -> deciding, but ranked second
        {"type": "way", "id": 81, "tags": {"name": "Plant fence", "barrier": "fence"},
         "geometry": [{"lat": 52.0009, "lon": 41.99}, {"lat": 52.0009, "lon": 42.01}]},
        # relation whose two outer ways leave a gap -> must not be treated as an area
        {"type": "relation", "id": 82, "tags": {"type": "multipolygon", "name": "Plant South"},
         "members": [{"type": "way", "ref": 5, "role": "outer",
                      "geometry": [{"lat": 51.97, "lon": 41.99}, {"lat": 51.97, "lon": 42.01}, {"lat": 51.98, "lon": 42.01}]},
                     {"type": "way", "ref": 6, "role": "outer",
                      "geometry": [{"lat": 51.98, "lon": 41.995}, {"lat": 51.97, "lon": 41.99}]}]},
    ],
    "N": [
        {"type": "relation", "id": 100, "tags": {"type": "multipolygon", "name": "Refinery N", "landuse": "industrial"},
         "bounds": {"minlat": 52.99, "minlon": 42.98, "maxlat": 53.02, "maxlon": 43.02}},
    ],
    "M": [
        {"type": "way", "id": 90, "tags": {"building": "yes", "addr:street": "Rue Saint-Jacques", "addr:housenumber": "277"},
         "geometry": box(48.85, 2.34, 0.0002)},
    ],
}
sites = m.site_table(events)
hint_rows = {r["site_id"]: r for r in csv.DictReader(open(hints, encoding="utf-8"))}
m.CACHE.mkdir(parents=True)
import hashlib
for sid, els in fixtures.items():
    s = sites[sid]; h = m.effective_hint(s, hint_rows[sid])
    q = m.build_query(h, s["lat_site"] or s["lat_coarse"], s["lon_site"] or s["lon_coarse"])
    key = hashlib.sha1(q.encode()).hexdigest()[:12]
    (m.CACHE / f"{sid}_{key}.json").write_text(json.dumps({"elements": els}), encoding="utf-8")
# follow-up response for N's relation: an L-shaped outline that does NOT contain the site point,
# although the bounding box does. With members fetched, the verdict must not be "agrees".
L = [{"lat": 52.99, "lon": 42.98}, {"lat": 52.99, "lon": 43.02}, {"lat": 52.995, "lon": 43.02},
     {"lat": 52.995, "lon": 42.985}, {"lat": 53.02, "lon": 42.985}, {"lat": 53.02, "lon": 42.98}, {"lat": 52.99, "lon": 42.98}]
relq = "[out:json][timeout:60][maxsize:67108864];\nrelation(id:100);\nout body geom;"
rkey = hashlib.sha1(relq.encode()).hexdigest()[:12]
(m.CACHE / f"N_rel_{rkey}.json").write_text(json.dumps({"elements": [
    {"type": "relation", "id": 100, "bounds": {"minlat": 52.99, "minlon": 42.98, "maxlat": 53.02, "maxlon": 43.02},
     "members": [{"type": "way", "ref": 7, "role": "outer", "geometry": L[:4]},
                 {"type": "way", "ref": 8, "role": "outer", "geometry": L[3:]}],
     "tags": {"type": "multipolygon", "name": "Refinery N"}}]}), encoding="utf-8")

m.cmd_check(types.SimpleNamespace(events=events, hints=hints, only=None, offline=True, max_candidates=1))
check = {r["site_id"]: r for r in csv.DictReader(open(m.OUT / "osm_site_check.csv", encoding="utf-8"))}
cands = list(csv.DictReader(open(m.OUT / "osm_candidates.csv", encoding="utf-8")))

def expect(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        expect.failed = True
expect.failed = False

expect(check["A"]["verdict"] == "agrees", f"A agrees (got {check['A']['verdict']})")
expect(check["A"]["best_osm"] == "way/20", f"A best is way/20 (got {check['A']['best_osm']})")
expect(not any(c["osm"] in ("node/10", "node/11", "node/12") for c in cands), "bus stop, shop node, fuel station dropped")
expect(not any(c["osm"] == "way/21" for c in m.candidates_for(sites["A"], m.effective_hint(sites["A"], hint_rows["A"]), {"elements": fixtures["A"]})),
       "box-corner feature outside the search circle dropped")
rel = [c for c in m.candidates_for(sites["A"], hint_rows["A"], {"elements": fixtures["A"]}) if c["osm"] == "relation/30"]
expect(rel and rel[0]["is_area"] is True, "split-way multipolygon rebuilt as an area")
expect(check["B"]["verdict"] == "proposal_only" and "ambiguous" in check["B"]["note"], f"B proposal + ambiguous ({check['B']['note']})")
expect(check["C"]["verdict"] == "disagrees", f"C disagrees (got {check['C']['verdict']})")
expect(not any(c["osm"] == "way/51" for c in cands), "street named НПЗ dropped")
expect(check["D"]["verdict"] == "skip_offshore", "D skipped")
expect(check["F"]["verdict"] == "disagrees" and check["F"]["deciding_osm"] == "way/61",
       f"F: unnamed polygon around the point does not confirm it (got {check['F']['verdict']}, {check['F']['deciding_osm']})")
expect(check["G"]["verdict"] == "agrees_tag_only", f"G: tag-only agreement is labelled (got {check['G']['verdict']})")
expect(check["K"]["verdict"] == "agrees" and check["K"]["deciding_osm"] == "way/81",
       f"K: closest named feature decides (got {check['K']['verdict']}, {check['K']['deciding_osm']})")
kshown = [c["osm"] for c in cands if c["site_id"] == "K"]
expect(kshown[0] == "way/80" and "way/81" in kshown and len(kshown) == 2,
       f"K: cut to 1 keeps the deciding candidate too (shown {kshown})")
expect(check["K"]["n_candidates"] == "3", f"K: verdict saw all 3 candidates (got {check['K']['n_candidates']})")
# re-run K with no cut to inspect the broken relation
krows = m.candidates_for(sites["K"], hint_rows["K"], {"elements": fixtures["K"]})
k82 = [c for c in krows if c["osm"] == "relation/82"][0]
expect(k82["is_area"] is False and "does not close" in k82["geom_warning"],
       f"K: open relation not treated as area, warned ({k82['is_area']}, {k82['geom_warning']!r})")
expect(check["N"]["verdict"] != "agrees" and "bounding box" not in check["N"]["note"],
       f"N: members fetched, real outline used, bbox no longer counts as containment (got {check['N']['verdict']}, {check['N']['note']!r})")
nd, still = m.complete_relations("N_nocache", {"elements": [dict(fixtures["N"][0])]}, offline=True)
expect(still == [100], "relation members unavailable -> reported as still missing")
expect(check["M"]["verdict"] == "agrees" and check["M"]["deciding_osm"] == "way/90", f"M: address match confirms (got {check['M']['verdict']})")
a20 = [c for c in cands if c["osm"] == "way/20"][0]
expect(a20["contains_site"] == "True" and float(a20["dist_to_site_km"]) == 0.0, "containment and zero distance")
expect(0.3 < float(a20["extent_km"]) < 0.7, f"extent of a ~0.9 km box is ~0.5 km (got {a20['extent_km']})")
# distance sanity: coarse point (51.48389,46.10528) to box around (51.43685,46.09028) ~ 5 km minus half-size
expect(4.0 < float(a20["dist_to_coarse_km"]) < 5.5, f"dist to coarse ~4.8 km (got {a20['dist_to_coarse_km']})")

# apply: accept A with y in the CSV, re-saved the way Excel does it (BOM, ';' separator),
# and B on the command line.
for c in cands:
    if c["osm"] == "way/20":
        c["accept"] = "y"
with open(m.OUT / "osm_candidates.csv", "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(cands[0].keys()), delimiter=";"); w.writeheader(); w.writerows(cands)

def apply_fails(accept):
    try:
        m.cmd_apply(types.SimpleNamespace(events=events, accept=accept))
    except SystemExit as e:
        return str(e)
    return ""
msg = apply_fails(["B=way/999"])
expect("not a candidate" in msg and "way/40" in msg, f"unknown OSM id rejected and options listed ({msg[:60]}...)")
msg = apply_fails(["K=way/80", "K=way/81"])
expect("two different candidates" in msg, f"two accepts for one site rejected ({msg[:70]}...)")
expect("use SITE=OSM" in apply_fails(["B:way/40"]), "malformed --accept rejected")
m.cmd_apply(types.SimpleNamespace(events=events, accept=["B=way/40"]))
rev = list(csv.DictReader(open(tmp / "data" / "events_reviewed.csv", encoding="utf-8")))
e1, e2, e3, e4 = rev[0], rev[1], rev[2], rev[3]  # A, A(control), B, C
expect(e1["osm_feature"] == "way/20" and e2["osm_feature"] == "way/20", "both events at site A updated (incl. control)")
expect(e1["site_coord_previous"].startswith("51.43685,46.09028"), "previous coordinates kept")
expect(e3["lat_site"] != "" and abs(float(e3["lat_site"]) - 45.05) < 0.01, "B filled from accepted candidate")
expect(e4["lat_site"] == "48.48917" and e4["osm_feature"] == "", "C untouched")
fp = json.loads((tmp / "data" / "site_footprints.geojson").read_text())
expect(len(fp["features"]) == 2, "two footprints written")
original = list(csv.DictReader(open(events, encoding="utf-8")))
expect(original[2]["lat_site"] == "", "events.csv not modified")

print("\nquery example:\n" + m.build_query(hint_rows["A"], "51.43685", "46.09028"))
sys.exit(1 if expect.failed else 0)
