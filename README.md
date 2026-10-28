# Satellite corroboration of news-detected events

**Question:** when the news reports a fire, explosion or strike at a place, can free satellite data (NASA FIRMS, Sentinel-2) independently confirm it, and what does it cost when the news location is only a city?

**Status (28 Oct 2026):** stages 1–8 done: pipeline, verdicts and the website (`08_WEBSITE/site`, not yet published). Licences and credits: `LICENSES_AND_ATTRIBUTION.md`.


**Website:** https://chris-krebs.github.io/Satellite_Imagery_Corroboration/ (built by `08_WEBSITE/build_site.py`, published by `.github/workflows/pages.yml` on every push to `main`).
**Licences:** code MIT (`LICENSE`); data CC BY 4.0, except OpenStreetMap-derived outlines (ODbL); see `DATA_LICENSE.md` and `LICENSES_AND_ATTRIBUTION.md`.
**Not in the repository** (see `.gitignore`): `.env`, API caches, full-size Sentinel-2 images, `03_QGIS`, `XX_GUIDELINES`.

---

## The pipeline at a glance

```
01 events -> 02 OSM check -> 04 FIRMS -> 05 persistent heat -> 07 Sentinel-2 -> 06 verdicts
100 events   site outlines    thermal     flares vs fire         images + blind    one verdict
                                                                 labels            per event
```

(06 runs last because it reads everything, including the Sentinel-2 labels. The numbers are historical.)

Every stage reads the previous stage's files and writes its own. Nothing is edited in place. Every API call is cached, so reruns are free.

| Folder | What it does | Main input | Main output | Command |
|---|---|---|---|---|
| `01_EVENTS` | The evaluation set: 100 events (74 incidents, 26 controls) | sources in the file | `events.csv` | (built once) |
| `02_OSM_CHECK` | Finds each site in OpenStreetMap; you accept the right outline | `data/events.csv`, `data/site_hints.csv` | `data/events_reviewed.csv`, `data/site_footprints.geojson` | `uv run src/osm_sites.py check` then `apply --accept S14=way/... ...` |
| `03_QGIS` | Your QGIS project for looking at everything | — | — | — |
| `04_FIRMS` | Downloads satellite fire detections around every event | `events_reviewed.csv`, footprints | `outputs/firms_detections.csv`, `outputs/firms_event_summary.csv` | `uv run src/firms.py run` |
| `05_PERSISTENT_HEAT` | Separates real fire from everyday industrial heat (flares) | FIRMS detections, `data/known_incidents.csv` | `outputs/heat_event_summary.csv`, `outputs/heat_cells.geojson` | `uv run src/persistent_heat.py run` / `sanity` |
| `07_SENTINEL2` | Before / after images, burn measure, blind labelling sheet | `events_reviewed.csv`, footprints, 05 labels | `outputs/panels/*.png`, `outputs/blind/*.png`, `outputs/s2_summary.csv`, `outputs/blind_results.csv` | `uv run src/sentinel2.py run`, then `uv run src/blind_analysis.py <label files>` |
| `06_VERDICTS` | One site-level and one city-level verdict per event | all of the above | `outputs/verdicts.csv`, `outputs/verdict_report.txt` | `uv run src/verdicts.py` |
| `08_WEBSITE` | Public website: results, map, method, cases, limits, one page per event | all outputs, `07_SENTINEL2/outputs/compressed` | `site/` (publish this folder) | `uv run build_site.py` |

Run commands from inside the stage folder. The FIRMS key lives in `.env` in this folder (never on GitHub; see `.gitignore`).

---

## What each stage does, in two sentences (for explaining it)

- **01 Events.** 100 historical events in five groups: large outdoor fires, industrial fires, port/ship fires, small/indoor events, and 26 *controls* (the same sites on ordinary days). Each event has a precise site point (where known) and a coarse city point, which is what a news geocoder would give.
- **02 OSM check.** Searches OpenStreetMap by name around each site and proposes matching features; a person picks the right one. 17 sites got a reviewed outline; the rest keep their original point, or have none.
- **04 FIRMS.** For each event, downloads all satellite heat detections in a box around it: 5 days around the event, plus a 30-day baseline. Days with no satellite data are recorded as gaps, so "nothing seen" is never confused with "no data".
- **05 Persistent heat.** Builds one clean year of history per site, maps where heat normally occurs (400 m grid), and labels each event detection *new*, *anomalous* or *background*. Only new + anomalous count as fire signal.
- **07 Sentinel-2.** For each event with a site point, picks a clear image before, the first image after and the first clear image after, and measures burn change (dNBR) in the site against a ring around it. The images were labelled blind (events and controls mixed, names hidden).
- **06 Verdicts.** Combines the evidence into one site-level verdict per event: *corroborated*, *ambiguous attribution*, *weak*, *not observable* or *not corroborated*. Separately it asks what a pipeline with only the city point would conclude: is the number of satellite passes with heat within 10 km of the city clearly above that city's own 30-day baseline?

### The verdict rules (06), in plain words

- **Timing window:** FIRMS signal must start between 3 h before and 36 h after the event (hour-precise events), or between 12 h before and 48 h after the event date (day-precise).
- **Corroborated:** FIRMS signal in the window; or a blind image label (fire, smoke, new damage) at a site *without* flares.
- **Ambiguous attribution:** the signal starts after another known incident at the same site (up to 4 days later).
- **Weak:** an image "fire" at a site with flares (flares look like fire in Sentinel-2 SWIR; the Garyville control got that label), or FIRMS signal only after the window.
- **Not observable:** nothing found and the site was ≥ 90% cloud in the first Sentinel-2 image after, with no clear image within 5 days.
- **Not corroborated:** nothing found although the data were there.
- Rules are fixed in `06_VERDICTS/data/verdict_rules.json`, with a note on what was seen before they were written and the one change made after the first run.

What breaks them: wrong site coordinates (02), cloud or very short fires (04), unlisted strikes inside a site's history year (05), flares in the images (07), and the cloud rule using a single 10:30 local-time image as a proxy for the whole window (06).

---

## Decisions made so far

1. **Sites without precise coordinates stay blank** (26 events). They get only the city-level verdict. This shows that news geocoding alone can't support site-level confirmation.
2. **17 OSM outlines accepted** (refineries, depots, Toropets, Marywilska, Sitakunda, ZOMZ, Saratoga, AES, Plastmass, RIA Pizza).
3. **Calibration / test split by site.** Thresholds are tuned on calibration sites only; results are quoted on test sites.
4. **Thresholds frozen on 27 Oct 2026**, before the first look at test results: `05_PERSISTENT_HEAT/data/frozen_thresholds.json`.
5. **Verdict rules fixed on 28 Oct 2026**: `06_VERDICTS/data/verdict_rules.json`. They were written after seeing some test cases (E002), so test results are semi-blind.

---

## Results (06_VERDICTS/outputs/verdict_report.txt)

**Site level** (events with a site point, excluding *not observable*):

| | Events corroborated | Controls "corroborated" (false alarms) |
|---|---|---|
| **Test half** | **8 of 16 (50%, 95% CI 28–72%)** | **0 of 10 (CI 0–28%)** |
| Calibration half | 9 of 21 (43%) | 0 of 16 |
| All | 17 of 37 (46%) | 0 of 26 (CI 0–13%) |

By type (all, observable): industrial 5/6, port/ship 5/10, large outdoor 7/16, small/indoor 0/5. Not observable (cloud): 11 events and 11 controls. One weak verdict on a control (Garyville image "fire" = flares).

**City level** (only the city point, no outline, no flare filter): events 13 of 74 (18%), controls 0 of 26.
Of the events with a site point, the site check corroborated 10 that the city check missed; the city check said yes once where the site said nothing (Kstovo E016, heat 2–4 km from the refinery that was also there the day before). For the 26 events without a site point, the city check is the only verdict: 5 of 26.

So: the precise site roughly doubles what can be confirmed (17 vs 8 of the same events with city points), and neither level produced a false alarm on the 26 controls. With 26 controls the false-alarm rate is only bounded to below ~13%.

**Example cases for the pitch:**

| Case | What it shows |
|---|---|
| Toropets arsenal (E017) | Clear hit: 512 signal detections in the window, first 0.9 h before the main blast |
| Yaroslavl refinery (E006 / E077) | The filter: control's flare detections → 0; strike kept |
| Engels depot (E021) | FIRMS saw nothing (cloud); the first clear Sentinel-2 image, 14 days later, was labelled new damage/scar. Treat with care: no clear image before, and the panel was seen before labelling |
| Proletarsk (E019) | No site point; city check confirms it (18 satellite passes with heat vs 1.8 expected) despite stubble burning around the town |
| Volgograd (E002) | The only FIRMS signal is a second attack 3 days later; the rule now discards it |
| Garyville (E020 / E083) | Flares look like fire in the images: event and control both labelled "fire" → weak |
| Kstovo (E016) | City check says yes from other heat in town; the site itself was under cloud |
| Moss Landing, Crocus, Deer Park (02) | OSM name matching picks the neighbouring facility |

---

## Open items

- [ ] **Check Moscow E005 ground truth** (date and scale) against primary news. Only source is the Wikipedia site history.
- [ ] Optional: automatic **cloud check** (Open-Meteo hourly cloud cover at overpass times) to replace the single-image cloud proxy.
- [ ] Optional: add the **Engels 6 June 2025** strike as an extra event (summer, same outline).
- [ ] Optional: more controls (26 bounds false alarms only to < 13%).
- [x] Website built (`08_WEBSITE`). Before publishing: check the repo URL in `build_site.py`, read the event pages for the cases, decide where the code repo lives.
- [ ] The website reports false alarms over *observable* controls (0 of 15, bound < 20%), not 0 of 26 as `verdict_report.txt` does: 11 controls were under cloud and could not produce a false alarm. Consider changing the report to match.
- [ ] E017 Toropets ground truth is partly circular (Wikipedia cites FIRMS); flagged on the site.

## Things to remember

- Never adjust event dates or coordinates because of what FIRMS shows (circular).
- After seeing test results, don't change thresholds or verdict rules; if you must, say so in the results.
- `check` in 02 overwrites `osm_candidates.csv`; `apply` rebuilds `events_reviewed.csv` from scratch: pass all accepted sites in one command.
- Runs with `--only` in 05 merge into existing outputs. In 04 they overwrite the outputs, but nothing is re-downloaded.
