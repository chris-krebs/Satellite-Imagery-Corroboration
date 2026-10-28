# Licences and attribution register

Checked 28 Oct 2026. Not legal advice; re-check the linked terms before publishing.

All sources used here are open: nothing has to be bought or applied for. What they do require is
attribution, a disclaimer link (NASA), and, for anything derived from OpenStreetMap, publishing that
derived data under the ODbL.

## 1. Data the pipeline uses

| Source | Used in | Licence / terms | What the website must do |
|---|---|---|---|
| **NASA FIRMS**, standard-processing (SP) archive: VIIRS S-NPP, NOAA-20, NOAA-21 (375 m) and MODIS Terra/Aqua (C6.1) | 04, 05, 06, map | NASA open data policy; no licence fee. LANCE disclaimer applies to third-party distribution | Acknowledgement text + link to the LANCE disclaimer + dataset DOIs (below) |
| **Copernicus Sentinel-2 L2A** (imagery dates 2021–2026) | 07, panels | Copernicus Sentinel data licence: free reproduction, distribution, adaptation | Caption/credit: "Contains modified Copernicus Sentinel data 2021–2026" (panels and dNBR are modified products) |
| **Element 84 Earth Search** / AWS Open Data registry (Sentinel-2 COGs: `sentinel-2-c1-l2a`, fallback `sentinel-2-l2a`) | 07 | Same Copernicus terms; registry asks for a citation | Cite: "Sentinel-2 Cloud-Optimized GeoTIFFs, accessed [dates] from https://registry.opendata.aws/sentinel-2-l2a-cogs" |
| **OpenStreetMap** via Overpass API | 02 (17 site outlines, candidate lists) | ODbL 1.0. Outlines extracted from OSM are a *Derivative Database* | "© OpenStreetMap contributors", linked to openstreetmap.org/copyright, on the map. Publish `site_footprints.geojson` and `osm_candidates.*` **under ODbL**, not CC BY |
| **GeoNames** | 01 (coarse city points, 99 events) | CC BY 4.0 | Credit "GeoNames (geonames.org), CC BY 4.0" in the data section |
| **Wikipedia** infobox coordinates | 01 (≈60 site points) | Coordinates are facts; article text is CC BY-SA 4.0 | Cite the article per event (already in `sources`). Do not copy article text into the site |
| **NOAA IncidentNews**, **US CSB** | 01 | US federal works, public domain | Cite |
| **News articles** (≈45 outlets) | 01 (ground truth) | Copyrighted | Link only. Paraphrase; no photos, screenshots or long quotes |

## 2. Website components

| Component | Licence / terms | What to do |
|---|---|---|
| MapLibre GL JS | BSD-3-Clause | Keep its licence notice in the bundled/CDN file (automatic via CDN) |
| VersaTiles basemap (tiles.versatiles.org) | Free, rate-limited, no guarantee; data is OSM (ODbL) + ESA WorldCover (CC BY 4.0) | Attribution "© OpenStreetMap contributors, ESA WorldCover" (VersaTiles styles add it automatically; add it yourself if you write your own style) |
| Esri World Imagery (used on dc-strikes-2026) | Esri terms. Per the OSM wiki summary (Feb 2025), free use outside OSM editing needs a free ArcGIS developer account/API key, no revenue, < 1 M tiles/month, and attribution | **Used on the map (28 Oct 2026)**, credited in the map attribution and on the page. Still open: the free ArcGIS developer key the terms ask for |

## 3. Licences for your own output

| Part | Suggested licence |
|---|---|
| Code (`*/src`, `*/tests`, website code) | MIT |
| Your own data (events list, verdicts, labels, heat cells, FIRMS summaries) | CC BY 4.0 |
| OSM-derived files (`02_OSM_CHECK/data/site_footprints.geojson`, `02_OSM_CHECK/outputs/osm_candidates.*`) | ODbL 1.0 (required) |
| Sentinel-2 panel images | Your choice for your work, but the Copernicus credit must stay with them |

Put a `LICENSE` (MIT) at the repo root and a `DATA_LICENSE.md` naming which files are CC BY 4.0 and
which are ODbL.

## 4. Ready-to-paste credits block

> **Data.** Active-fire detections: NASA FIRMS. We acknowledge the use of data from NASA's Fire
> Information for Resource Management System (FIRMS) (https://www.earthdata.nasa.gov/firms), part of
> NASA's Earth Science Data and Information System (ESDIS). Products: VIIRS 375 m active fire,
> S-NPP (doi:10.5067/VIIRS/VNP14IMG.002), NOAA-20 (doi:10.5067/VIIRS/VJ114IMG.002), NOAA-21
> (doi:10.5067/VIIRS/VJ214IMG.002); MODIS Collection 6.1 active fire (Giglio, Schroeder, Hall &
> Justice, MODIS C6/C6.1 Active Fire Product User's Guide, 2021). FIRMS data are provided "as is"; see the
> [LANCE disclaimer](https://www.earthdata.nasa.gov/data/projects/lance/disclaimer).
> Imagery: contains modified Copernicus Sentinel data 2021–2026, accessed via Element 84 Earth Search
> (registry.opendata.aws/sentinel-2-l2a-cogs). Site outlines and basemap: © OpenStreetMap
> contributors (ODbL); ESA WorldCover (CC BY 4.0). Place coordinates: GeoNames (CC BY 4.0) and
> Wikipedia. Event sources: linked per event.
>
> **Licences.** Code: MIT. Data: CC BY 4.0, except OSM-derived site outlines (ODbL 1.0).

## 5. Open items

- [ ] Confirm the three VIIRS DOIs against the FIRMS product pages (pattern confirmed for NOAA-20:
      doi:10.5067/VIIRS/VJ114IMG.002). MODIS MCD14ML has no dataset DOI listed; cite the user guide.
- [ ] `07_SENTINEL2/outputs/s2_summary.csv`: the `collections` column is empty for 89 of 100 rows, so
      the file does not record which collection each image came from. Fix before publishing provenance.
- [ ] Esri basemap is on the site: register a free ArcGIS developer account/key, as the terms require.
- [ ] Make sure `ground_truth_basis` text is your paraphrase, not copied article sentences.
- [ ] The LANCE disclaimer says FIRMS is not recommended for "conditions at a local scale". The site
      uses it at site level, so the method section has to address that directly, not just link the disclaimer.

## Sources checked

- Copernicus Sentinel data licence: https://ewds.climate.copernicus.eu/licences/ec-sentinel
- AWS registry, Sentinel-2 COGs: https://registry.opendata.aws/sentinel-2-l2a-cogs/
- FIRMS FAQ: https://www.earthdata.nasa.gov/data/tools/firms/faq
- LANCE citation/acknowledgement: https://www.earthdata.nasa.gov/learn/find-data/near-real-time/citation
- LANCE disclaimer: https://www.earthdata.nasa.gov/data/projects/lance/disclaimer
- VJ114IMG v002 (DOI): https://www.earthdata.nasa.gov/data/catalog/lpcloud-vj114img-002
- MODIS C6/C6.1 Fire User Guide: https://earthdata.nasa.gov/s3fs-public/2023-09/MODIS_C6_C6.1_Fire_User_Guide_1.0.pdf
- OSMF attribution guidelines: https://osmfoundation.org/wiki/Attribution
- OSM wiki on Esri imagery: https://wiki.openstreetmap.org/wiki/Esri
- VersaTiles tile server: https://docs.versatiles.org/guides/use_tiles_versatiles_org.html
- GeoNames licence (via geocode.earth): https://geocode.earth/docs/reference/data_sources/
