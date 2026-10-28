# Data licences

| Files | Licence | Attribution |
|---|---|---|
| `data/events.csv`, `data/fire_detections.csv`, `data/heat_cells.geojson`, `data/map.json`, `data/detections.json`, `data/verdict_rules.json`, `data/frozen_thresholds.json`, `data/verdict_report.txt` | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | Kristof Krebs, *Event corroboration with open-source data streams* (2026) |
| `data/site_outlines_odbl.geojson`, and in `pipeline/02_OSM_CHECK/`: `data/site_footprints.geojson`, `outputs/osm_candidates.*`, `outputs/osm_site_check.csv` | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) | © OpenStreetMap contributors |
| All other data files in `pipeline/` (event list, FIRMS and heat outputs, verdicts, image labels) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | Kristof Krebs, *Event corroboration with open-source data streams* (2026) |
| `img/` (Sentinel-2 panels) | CC BY 4.0 for the author's work | Contains modified Copernicus Sentinel data 2021–2026 |

Third-party sources inside these files keep their own terms:

- FIRMS detections: NASA FIRMS, part of NASA's Earth Science Data and Information System (ESDIS).
  Provided "as is"; see the [LANCE disclaimer](https://www.earthdata.nasa.gov/data/projects/lance/disclaimer).
- City coordinates (`lat_coarse`, `lon_coarse`): [GeoNames](https://www.geonames.org/), CC BY 4.0.
- Site coordinates: Wikipedia and OpenStreetMap (ODbL).
- News and official sources are linked, not reproduced.
