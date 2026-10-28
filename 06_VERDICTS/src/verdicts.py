# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""One verdict per event, from all the evidence gathered in stages 02-07. No downloads.

    uv run src/verdicts.py

Two verdicts per event:

SITE LEVEL (events with a site point)
  corroborated            FIRMS fire signal (stage 05: new/anomalous, at the site) starting inside
                          the timing window and not better explained by another known incident;
                          or a positive blind image label at a site without persistent heat.
  ambiguous_attribution   the only signal could belong to another known incident at the same site.
  weak                    only weak evidence: a visual 'fire' at a site with flares (flares look
                          like fire in SWIR: one control was labelled 'active fire' this way), or
                          FIRMS signal that starts outside the timing window with nothing else.
  not_observable          nothing found, and the sky was most likely closed (Sentinel-2: site
                          covered >= 90% in the first image after, no clear image within 5 days),
                          or FIRMS data incomplete.
  not_corroborated        nothing found although the data were there.
  not_assessable_no_site  no site point (decision 1): only the city-level verdict applies.

CITY LEVEL (every event; what a pipeline with only the geocoded city point would conclude)
  Distinct satellite overpasses with a raw FIRMS detection within 10 km of the city point,
  inside the timing window, compared with the same area's own 30-day baseline rate.
  corroborated if the count is significantly above baseline (Poisson, p < 0.01), else
  not_corroborated. No flare filter, no site outline: that is the point of the comparison.

Rules and thresholds: data/verdict_rules.json (written on first run, then read).
Outputs: outputs/verdicts.csv (every event, every input, both verdicts, reasons)
         outputs/verdict_report.txt (the tables below)
"""
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # 06_VERDICTS
PROJECT = ROOT.parent
P = {
    "events": PROJECT / "02_OSM_CHECK" / "data" / "events_reviewed.csv",
    "firms_summary": PROJECT / "04_FIRMS" / "outputs" / "firms_event_summary.csv",
    "firms_detections": PROJECT / "04_FIRMS" / "outputs" / "firms_detections.csv",
    "heat_summary": PROJECT / "05_PERSISTENT_HEAT" / "outputs" / "heat_event_summary.csv",
    "heat_labels": PROJECT / "05_PERSISTENT_HEAT" / "outputs" / "labelled_detections.csv",
    "heat_cells": PROJECT / "05_PERSISTENT_HEAT" / "outputs" / "heat_cells.csv",
    "incidents": PROJECT / "05_PERSISTENT_HEAT" / "data" / "known_incidents.csv",
    "s2": PROJECT / "07_SENTINEL2" / "outputs" / "s2_summary.csv",
    "blind": PROJECT / "07_SENTINEL2" / "outputs" / "blind_results.csv",
}
RULES_FILE = ROOT / "data" / "verdict_rules.json"
OUT = ROOT / "outputs"

DEFAULT_RULES = {
    "written_on": None,
    "disclosure": ("Written after stages 04-07 had been run and partly inspected, including test-half sites: "
                   "the attribution rule was motivated by Volgograd E002 (test half) and the flare caveat for "
                   "visual labels by the Garyville control E083 (calibration). Treat test-half verdicts as "
                   "semi-blind. One change after the first run (28 Oct 2026): the attribution check now also "
                   "applies to signals after the timing window, because E002 had come out 'weak' on the other "
                   "attack's fire, the case the rule was written for. Do not change these rules after reading "
                   "verdict_report.txt; if you must, "
                   "say so in the results."),
    "timing_window_hours": {"hour": [-3, 36], "day": [-12, 48]},
    "attribution_days_after": 4,
    "attribution_lead_hours": 6,
    "cloud_first_after_min": 0.90,
    "cloud_clear_after_max_days": 5,
    "flare_site_radius_extra_km": 0.5,
    "city_radius_km": 10.0,
    "city_p_value": 0.01,
    "baseline_days": 30,
}


# ------------------------------------------------------------------ helpers
def read(path):
    p = Path(path)
    if not p.exists() or not p.stat().st_size:
        return []
    return list(csv.DictReader(open(p, encoding="utf-8")))


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def t0_of(event_time):
    v = event_time.strip()
    if "T" in v:
        return datetime.strptime(v.rstrip("Z"), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    return datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def split_of(site_id):                               # same rule as stage 05
    return "calibration" if int(hashlib.md5(site_id.encode()).hexdigest(), 16) % 2 == 0 else "test"


def haversine_km(a, b, c, d):
    r = 6371.0088
    p1, p2 = math.radians(a), math.radians(c)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def poisson_sf(k, lam):
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term, cdf = math.exp(-lam), 0.0
    for i in range(k):
        if i > 0:
            term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def incident_dates(events, incidents_rows):
    """site_id -> list of (start, end) dates of known incidents (events list + known_incidents.csv)."""
    out = defaultdict(list)
    for e in events:
        if e["category"] != "negative_control":
            d = t0_of(e["event_time_utc"]).date()
            out[e["site_id"]].append((d, d, e["event_id"]))
    for r in incidents_rows:
        v = r["date"].strip()
        if len(v) == 7:
            y, m = int(v[:4]), int(v[5:7])
            a = datetime(y, m, 1).date()
            b = (datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)).date()
        else:
            a = b = datetime.strptime(v, "%Y-%m-%d").date()
        out[r["site_id"]].append((a, b, f"incident {v}"))
    return out


# ------------------------------------------------------------------ rules
def site_verdict(ev, R, sig_hours, firms_complete, visual, flare_site, s2, other_incidents, seen_before=False):
    """Return (verdict, evidence, reason). sig_hours: hours from event time of site signal detections."""
    lo, hi = R["timing_window_hours"][ev["time_precision"] if ev["time_precision"] in ("hour", "day") else "day"]
    t0 = t0_of(ev["event_time_utc"])
    in_win = sorted(h for h in sig_hours if lo <= h <= hi)
    late = sorted(h for h in sig_hours if h > hi)
    reasons = []

    def explained_by(first_h):
        """Name of another known incident that started before this signal, else None."""
        first = t0 + timedelta(hours=first_h)
        for a, b, name in other_incidents:
            start = datetime(a.year, a.month, a.day, tzinfo=timezone.utc) - timedelta(hours=R["attribution_lead_hours"])
            if t0.date() < a <= (t0 + timedelta(days=R["attribution_days_after"])).date() and first >= start:
                return name
        return None

    firms_ok = bool(in_win)
    attributable = True
    if firms_ok:
        reasons.append(f"FIRMS signal from {in_win[0]:+.1f} h ({len(in_win)} detections in window)")
        other = explained_by(in_win[0])
        if other:
            attributable = False
            reasons.append(f"first signal {in_win[0]:+.0f} h comes after another incident at the site ({other})")
    if late and not firms_ok:
        other = explained_by(late[0])
        if other:
            reasons.append(f"FIRMS signal only from {late[0]:+.0f} h, after another incident at the site ({other}); not counted")
            late = []

    vis_pos = visual in ("active_fire", "smoke", "new_damage_or_scar")
    vis_strong = vis_pos and not flare_site
    if vis_pos:
        reasons.append(f"image labelled {visual}" + (" at a site with flares (weak)" if flare_site else "")
                       + (" (image seen before labelling)" if seen_before else ""))

    if firms_ok and attributable and vis_strong:
        return "corroborated", "FIRMS+image", "; ".join(reasons)
    if firms_ok and attributable:
        return "corroborated", "FIRMS", "; ".join(reasons)
    if vis_strong:
        return "corroborated", "image", "; ".join(reasons)
    if firms_ok and not attributable:
        return "ambiguous_attribution", "FIRMS", "; ".join(reasons)
    if vis_pos or late:
        if late:
            reasons.append(f"FIRMS signal only from {late[0]:+.0f} h, after the timing window")
        return "weak", "image" if vis_pos else "late FIRMS", "; ".join(reasons)
    lead = "; ".join(reasons) + "; " if reasons else ""
    if not firms_complete:
        return "not_observable", "", lead + "FIRMS data incomplete"
    fa_cloud, ca_days = f(s2.get("first_after_cloud")), f(s2.get("clear_after_days"))
    if fa_cloud is not None and fa_cloud >= R["cloud_first_after_min"] and (ca_days is None or ca_days > R["cloud_clear_after_max_days"]):
        return "not_observable", "", lead + (f"site {fa_cloud:.0%} cloud in the first Sentinel-2 image after; "
                                      f"first clear image {'never' if ca_days is None else f'+{ca_days:.0f} d'}")
    return "not_corroborated", "", lead + "no signal although data were available" + (
        "" if s2 else " (no Sentinel-2 cloud information)")


def city_verdict(ev, R, overpass_hours_event, n_overpasses_baseline):
    lo, hi = R["timing_window_hours"][ev["time_precision"] if ev["time_precision"] in ("hour", "day") else "day"]
    k = len({h for h in overpass_hours_event if lo <= h <= hi})
    win_days = (hi - lo) / 24.0
    lam = n_overpasses_baseline / R["baseline_days"] * win_days
    p = poisson_sf(k, lam)
    v = "corroborated" if k > 0 and p < R["city_p_value"] else "not_corroborated"
    return v, k, round(lam, 2), p


# ------------------------------------------------------------------ main
def main():
    OUT.mkdir(exist_ok=True)
    if RULES_FILE.exists():
        R = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    else:
        R = dict(DEFAULT_RULES, written_on=datetime.now(timezone.utc).date().isoformat())
        RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
        RULES_FILE.write_text(json.dumps(R, indent=2), encoding="utf-8")
        print(f"rules written to {RULES_FILE} - they are now fixed")

    events = read(P["events"])
    firms_sum = {r["event_id"]: r for r in read(P["firms_summary"])}
    heat_sum = {r["event_id"]: r for r in read(P["heat_summary"])}
    s2 = {r["event_id"]: r for r in read(P["s2"])}
    blind = {r["event_id"]: r for r in read(P["blind"])}
    incidents = incident_dates(events, read(P["incidents"]))

    # site signal timing: stage 05 labels where available, else raw stage-04 site matches (ships)
    sig = defaultdict(list)
    for r in read(P["heat_labels"]):
        if r.get("window", "event") == "event" and r["site_match"] == "True" and r["signal"] == "True":
            sig[r["event_id"]].append(f(r["hours_from_event"]))
    raw_site, city_ev, city_bl = defaultdict(list), defaultdict(list), defaultdict(set)
    for r in read(P["firms_detections"]):
        key = f"{r['satellite']}@{r['acq_utc']}"
        if r["window"] == "event":
            if r["site_match"] == "True":
                raw_site[r["event_id"]].append(f(r["hours_from_event"]))
            if r["dist_coarse_km"] and f(r["dist_coarse_km"]) <= R["city_radius_km"]:
                city_ev[r["event_id"]].append((f(r["hours_from_event"]), key))
        elif r["window"] == "baseline" and r["dist_coarse_km"] and f(r["dist_coarse_km"]) <= R["city_radius_km"]:
            city_bl[r["event_id"]].add(key)

    # sites with persistent heat (flares) at or near the site
    events_by_site = {e["site_id"]: e for e in events if e["lat_site"]}
    flare_sites = set()
    for c in read(P["heat_cells"]):
        if c["persistent"] != "True":
            continue
        e = events_by_site.get(c["site_id"])
        if not e:
            continue
        r_km = (f(e.get("site_uncertainty_km"), 0.5) or 0.5) + R["flare_site_radius_extra_km"]
        if c["in_footprint"] == "True" or haversine_km(float(c["lat"]), float(c["lon"]), float(e["lat_site"]), float(e["lon_site"])) <= r_km:
            flare_sites.add(c["site_id"])

    rows = []
    for ev in events:
        eid, sid = ev["event_id"], ev["site_id"]
        fs = firms_sum.get(eid, {})
        complete = bool(fs) and not fs.get("failed_requests", "").strip() and fs.get("event_coverage") != "NONE"
        others = [x for x in incidents.get(sid, []) if x[2] != eid]
        if not ev["lat_site"]:
            sv, evid, reason, source = "not_assessable_no_site", "", "no site point (kept blank on purpose)", ""
        else:
            if eid in heat_sum:
                hours, source = [h for h in sig[eid] if h is not None], "05 filtered"
            else:
                hours, source = [h for h in raw_site[eid] if h is not None], "04 raw (no flare filter)"
            b = blind.get(eid, {})
            sv, evid, reason = site_verdict(ev, R, hours, complete, b.get("label", ""), sid in flare_sites,
                                            s2.get(eid, {}) if s2.get(eid, {}).get("status") not in (None, "", "skipped_no_site_point", "skipped_position_too_vague") else {},
                                            others, b.get("seen_before") == "True")
        hours_city = {(h, k) for h, k in city_ev[eid] if h is not None}
        cv, ck, clam, cp = city_verdict(ev, R, [h for h, _ in hours_city], len(city_bl[eid]))
        b = blind.get(eid, {})
        rows.append({
            "event_id": eid, "site_id": sid, "split": split_of(sid), "category": ev["category"],
            "ground_truth": ev["ground_truth"], "place_name": ev["place_name"], "event_time_utc": ev["event_time_utc"],
            "time_precision": ev["time_precision"], "site_verdict": sv, "site_evidence": evid, "site_reason": reason,
            "signal_source": source if ev["lat_site"] else "", "flare_site": sid in flare_sites,
            "visual_label": b.get("label", ""), "visual_seen_before": b.get("seen_before", ""),
            "s2_status": s2.get(eid, {}).get("status", ""), "firms_complete": complete,
            "city_verdict": cv, "city_overpasses_in_window": ck, "city_expected_from_baseline": clam,
            "city_p_value": round(cp, 4),
        })

    cols = list(rows[0].keys())
    with open(OUT / "verdicts.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader(); w.writerows(rows)
    report = make_report(rows, R)
    (OUT / "verdict_report.txt").write_text(report, encoding="utf-8")
    print(report)


def make_report(rows, R):
    L = []
    pct = lambda k, n: "-" if n == 0 else f"{k}/{n} = {k / n:.0%}"
    ci = lambda k, n: "" if n == 0 else "  (95% CI {:.0%}-{:.0%})".format(*wilson(k, n))
    L.append("VERDICTS  (rules: data/verdict_rules.json)")
    L.append(R.get("disclosure", ""))
    L.append("")
    order = ["corroborated", "ambiguous_attribution", "weak", "not_observable", "not_corroborated", "not_assessable_no_site"]
    for split in ("test", "calibration", "all"):
        sel = [r for r in rows if split == "all" or r["split"] == split]
        L.append(f"== SITE LEVEL, {split} ==")
        L.append(f"  {'':24}" + "".join(f"{o[:14]:>16}" for o in order))
        for grp in ("events", "controls"):
            g = [r for r in sel if (r["category"] == "negative_control") == (grp == "controls")]
            c = Counter(r["site_verdict"] for r in g)
            L.append(f"  {grp:24}" + "".join(f"{c[o]:>16}" for o in order))
        ev = [r for r in sel if r["category"] != "negative_control" and r["site_verdict"] != "not_assessable_no_site"]
        obs = [r for r in ev if r["site_verdict"] != "not_observable"]
        ctl = [r for r in sel if r["category"] == "negative_control"]
        k = sum(r["site_verdict"] == "corroborated" for r in obs)
        fa = sum(r["site_verdict"] == "corroborated" for r in ctl)
        L.append(f"  events corroborated (where observable): {pct(k, len(obs))}{ci(k, len(obs))}")
        L.append(f"  controls 'corroborated' (false alarms):  {pct(fa, len(ctl))}{ci(fa, len(ctl))}")
        L.append("")
    L.append("== SITE LEVEL by category (all splits; observable events) ==")
    for cat in sorted({r["category"] for r in rows} - {"negative_control"}):
        g = [r for r in rows if r["category"] == cat and r["site_verdict"] not in ("not_assessable_no_site", "not_observable")]
        k = sum(r["site_verdict"] == "corroborated" for r in g)
        no = sum(r["site_verdict"] == "not_observable" for r in rows if r["category"] == cat)
        L.append(f"  {cat:22} {pct(k, len(g)):>14}   (+{no} not observable)")
    L.append("")
    L.append("== CITY LEVEL vs SITE LEVEL (what a city-point pipeline would say) ==")
    for grp in ("events", "controls"):
        g = [r for r in rows if (r["category"] == "negative_control") == (grp == "controls")]
        k = sum(r["city_verdict"] == "corroborated" for r in g)
        L.append(f"  {grp:9} city-level corroborated: {pct(k, len(g))}{ci(k, len(g))}")
    both = [r for r in rows if r["category"] != "negative_control" and r["site_verdict"] not in ("not_assessable_no_site",)]
    agree = Counter((r["site_verdict"] == "corroborated", r["city_verdict"] == "corroborated") for r in both)
    L.append(f"  events with a site point: site yes & city yes {agree[(True, True)]}, site yes & city no {agree[(True, False)]}, "
             f"site no & city yes {agree[(False, True)]}, both no {agree[(False, False)]}")
    nosite = [r for r in rows if r["site_verdict"] == "not_assessable_no_site" and r["category"] != "negative_control"]
    L.append(f"  events WITHOUT a site point: city-level corroborated {pct(sum(r['city_verdict'] == 'corroborated' for r in nosite), len(nosite))}"
             " - the only verdict available for them")
    L.append("")
    L.append("== Every event ==")
    for r in sorted(rows, key=lambda r: (r["category"] == "negative_control", r["category"], r["event_id"])):
        L.append(f"  {r['event_id']:5} {r['split'][:5]:5} {r['category'][:14]:14} {r['place_name'][:30]:30} "
                 f"site: {r['site_verdict']:22} city: {r['city_verdict']:16} {r['site_reason'][:90]}")
    return "\n".join(L)


if __name__ == "__main__":
    main()
