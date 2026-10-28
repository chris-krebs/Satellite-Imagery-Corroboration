/* Progressive enhancement only: every number and event is already in the HTML.
   This adds the result tabs, the table filters and the map. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const getJSON = (u) => fetch(u).then((r) => { if (!r.ok) throw new Error(u + " " + r.status); return r.json(); });

  const C = {
    bg: "#000000", text: "#ededed", text2: "#a3a3a3", text3: "#7a7a7a",
    fire: "#d0645a", anom: "#d89a50", weak: "#b0aa5c", ambig: "#a985b5", cloud: "#7c98b2", none: "#6d747c",
    outline: "#4fd1e0",
  };
  const VCOL = ["match", ["get", "v"], "corroborated", C.fire, "ambiguous_attribution", C.ambig, "weak", C.weak,
    "not_observable", C.cloud, "not_corroborated", C.none, C.text3];
  const SV = { corroborated: "Corroborated", ambiguous_attribution: "Ambiguous attribution", weak: "Weak",
    not_observable: "Not observable", not_corroborated: "Not corroborated", not_assessable_no_site: "No site point" };
  const CV = { corroborated: "Corroborated", not_corroborated: "Not corroborated" };
  const CATS = { industrial_fire: "Industrial fire", large_outdoor_fire: "Large outdoor fire", port_ship_fire: "Port or ship fire",
    small_or_indoor: "Small or indoor", negative_control: "Control" };
  const pill = (v, city) => `<span class="pill v-${esc(v)}">${esc((city ? CV : SV)[v] || v)}</span>`;

  // ------------------------------------------------------------ result tabs
  const tabs = $(".tabs");
  if (tabs) {
    tabs.hidden = false;
    tabs.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-split]");
      if (!b) return;
      $$("button", tabs).forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      $$("[data-split-panel]").forEach((p) => { p.hidden = p.dataset.splitPanel !== b.dataset.split; });
    });
  }

  // ------------------------------------------------------------ table filters
  const form = $("#filters");
  const rows = $$("#events tbody tr");
  function filter() {
    const f = Object.fromEntries(new FormData(form));
    let n = 0;
    rows.forEach((r) => {
      const ok = (!f.cat || r.dataset.cat === f.cat) && (!f.sv || r.dataset.sv === f.sv) &&
        (!f.cv || r.dataset.cv === f.cv) && (!f.split || r.dataset.split === f.split);
      r.hidden = !ok; if (ok) n++;
    });
    $("#count").textContent = `${n} of ${rows.length} events`;
  }
  if (form) { form.hidden = false; form.addEventListener("change", filter); filter(); }

  // whole row opens the event page (links and the Map button keep their own behaviour)
  rows.forEach((r) => r.addEventListener("click", (e) => {
    if (e.target.closest("a, button") || window.getSelection().toString()) return;
    const link = r.querySelector("a.open-link");
    if (!link) return;
    if (e.metaKey || e.ctrlKey) window.open(link.href, "_blank"); else location.href = link.href;
  }));

  // ------------------------------------------------------------ map
  const mapEl = $("#map");
  if (!mapEl) return;
  let map, M, DET = null, sel = { site: null, event: null };
  const vis = { sites: true, nosite: true, city: false, outlines: true };
  const GROUPS = {
    sites: ["sites"], nosite: ["nosite", "nosite-ring"], city: ["city-line", "city-pt"], outlines: ["outline-fill", "outline-line"],
  };

  const raster = (tiles, attribution, maxzoom) => ({
    version: 8,
    sources: { base: { type: "raster", tiles, tileSize: 256, maxzoom, attribution } },
    layers: [{ id: "bg", type: "background", paint: { "background-color": C.bg } }, { id: "base", type: "raster", source: "base" }],
  });
  const BASES = {
    dark: async () => { const s = await getJSON("data/basemap-dark.json"); delete s.terrain; return s; },
    osm: async () => raster(["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors', 19),
    satellite: async () => raster(["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
      'Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community', 19),
  };

  const empty = { type: "FeatureCollection", features: [] };
  function addOverlays() {
    const src = (id, data) => { if (!map.getSource(id)) map.addSource(id, { type: "geojson", data }); };
    src("sites", M.sites); src("nosite", M.nosite); src("rings", M.rings); src("city", M.city); src("offsets", M.offsets);
    src("outlines", "data/site_outlines_odbl.geojson"); src("cells", "data/heat_cells.geojson"); src("dets", detFC(sel.event));
    const add = (l) => { if (!map.getLayer(l.id)) map.addLayer(l); };
    const selSite = sel.site || "";
    add({ id: "cells", type: "fill", source: "cells", filter: ["==", ["get", "site_id"], selSite],
      paint: { "fill-color": ["case", ["get", "persistent"], "rgba(237,237,237,0.30)", "rgba(237,237,237,0.05)"],
        "fill-outline-color": ["case", ["get", "persistent"], "#9e9e9e", "#5a5a5a"] } });
    add({ id: "outline-fill", type: "fill", source: "outlines", minzoom: 8, paint: { "fill-color": C.outline, "fill-opacity": 0.08 } });
    add({ id: "outline-line", type: "line", source: "outlines", minzoom: 8, paint: { "line-color": C.outline, "line-width": 1.5 } });
    add({ id: "nosite-ring", type: "line", source: "rings", filter: ["==", ["get", "nosite"], true],
      paint: { "line-color": C.text3, "line-width": 1, "line-dasharray": [2, 2] } });
    add({ id: "sel-ring", type: "line", source: "rings", filter: ["==", ["get", "e"], sel.event || ""],
      paint: { "line-color": C.text, "line-width": 1.5, "line-dasharray": [3, 2] } });
    add({ id: "city-line", type: "line", source: "offsets", paint: { "line-color": C.text2, "line-width": 1, "line-opacity": 0.8 } });
    add({ id: "city-pt", type: "circle", source: "city", paint: { "circle-radius": 3, "circle-color": C.text, "circle-stroke-color": C.bg, "circle-stroke-width": 1 } });
    add({ id: "dets", type: "circle", source: "dets",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 2.5, 11, 4, 14, 7],
        "circle-color": ["match", ["get", "l"], "new", C.fire, "anomalous", C.anom, "#8a8a8a"],
        "circle-opacity": ["match", ["get", "l"], "background", 0.75, 0.9],
        "circle-stroke-color": C.bg, "circle-stroke-width": 0.75,
      } });
    add({ id: "nosite", type: "circle", source: "nosite",
      paint: { "circle-radius": 6, "circle-color": "rgba(0,0,0,0)",
        "circle-stroke-color": ["match", ["get", "cv"], "corroborated", C.fire, C.text2], "circle-stroke-width": 2 } });
    add({ id: "sites", type: "circle", source: "sites",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 1, 5, 6, 7, 12, 9],
        "circle-color": VCOL,
        "circle-stroke-color": ["case", ["==", ["get", "site"], selSite], C.text, C.bg],
        "circle-stroke-width": ["case", ["==", ["get", "site"], selSite], 2.5, 1.5],
      } });
    applyVisibility();
  }
  function applyVisibility() {
    Object.entries(GROUPS).forEach(([g, ids]) => ids.forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", vis[g] ? "visible" : "none")));
  }
  function detFC(eid) {
    if (!eid || !DET || !DET[eid]) return empty;
    return { type: "FeatureCollection", features: DET[eid].map(([lon, lat, l, h, frp, sat, utc]) => ({
      type: "Feature", properties: { l, h, frp, sat, utc }, geometry: { type: "Point", coordinates: [lon, lat] } })) };
  }
  function refreshSelection() {
    if (!map || !map.getLayer("sites")) return;
    const s = sel.site || "";
    map.setFilter("cells", ["==", ["get", "site_id"], s]);
    map.setFilter("sel-ring", ["==", ["get", "e"], sel.event || ""]);
    map.setPaintProperty("sites", "circle-stroke-color", ["case", ["==", ["get", "site"], s], C.text, C.bg]);
    map.setPaintProperty("sites", "circle-stroke-width", ["case", ["==", ["get", "site"], s], 2.5, 1.5]);
    map.getSource("dets").setData(detFC(sel.event));
  }

  function evCard(id) {
    const e = M.events[id];
    const det = e.site ? (e.ndet ? `<p class="det-sum">${e.ndet} FIRMS detections within 5 km in the event window. At the site: ${e.sig} fire, ${e.bg} usual heat.</p>`
      : `<p class="det-sum">No FIRMS detections within 5 km in the event window.</p>`) : "";
    return `<li class="${id === sel.event ? "sel" : ""}"><button type="button" data-ev="${id}">
      <span class="ev-top"><b>${id}</b> ${pill(e.sv)} <span class="muted">${esc(e.date.split(",")[0])} · ${esc(CATS[e.cat])}</span></span>
      <span class="ev-reason">${esc(e.reason)}</span></button>
      ${id === sel.event ? det : ""}
      <p class="ev-link"><a href="events/${id}.html">Evidence page →</a> · city level ${pill(e.cv, true)}</p></li>`;
  }
  function renderDetail() {
    const d = $("#detail");
    const legend = $(".map-legend", d)?.outerHTML || "";
    if (sel.site) {
      const s = M.siteinfo[sel.site];
      d.innerHTML = `<h3>${esc(s.name)}</h3><p class="site-meta">${s.events.length} event${s.events.length > 1 ? "s" : ""} at this site · ${s.outline ? "OSM outline" : "site point, no outline"}</p>
        <ul>${s.events.map(evCard).join("")}</ul>${legend}`;
    } else if (sel.event) {
      const e = M.events[sel.event];
      d.innerHTML = `<h3>${esc(e.name)}</h3><p class="site-meta">No site point. Only the city check applies. Ring: ${M.city_km} km around ${esc(e.place)}.</p>
        <ul>${evCard(sel.event)}</ul><p class="det-sum">City check: ${esc(e.cpass)} passes with heat in the window. Expected: ${esc(e.cexp)}.</p>${legend}`;
    }
    $$("button[data-ev]", d).forEach((b) => b.addEventListener("click", () => select(b.dataset.ev, false)));
  }

  async function select(eid, fly = true) {
    const e = M.events[eid];
    if (!e) return;
    if (!DET) { try { DET = await getJSON("data/detections.json"); } catch (err) { DET = {}; } }
    sel = { site: e.site, event: eid };
    refreshSelection();
    renderDetail();
    if (!fly) return;
    const b = new maplibregl.LngLatBounds();
    if (e.site) {
      const f = M.sites.features.find((x) => x.properties.site === e.site);
      b.extend(f.geometry.coordinates);
      (DET[eid] || []).forEach(([lon, lat]) => b.extend([lon, lat]));
      const c = f.geometry.coordinates, k = 0.03;
      b.extend([c[0] - k, c[1] - k]); b.extend([c[0] + k, c[1] + k]);
    } else {
      const r = M.rings.features.find((x) => x.properties.e === eid);
      r.geometry.coordinates[0].forEach((p) => b.extend(p));
    }
    map.fitBounds(b, { padding: 50, maxZoom: 13.5, duration: 900 });
  }

  async function buildMap() {
    M = await getJSON("data/map.json");
    const b = new maplibregl.LngLatBounds();
    M.sites.features.concat(M.nosite.features).forEach((f) => b.extend(f.geometry.coordinates));
    map = new maplibregl.Map({
      container: "map", style: await BASES.dark(), bounds: b, fitBoundsOptions: { padding: 40 },
      cooperativeGestures: true, attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.on("style.load", addOverlays);
    map.on("click", "sites", (ev) => {
      const s = M.siteinfo[ev.features[0].properties.site];
      const first = s.events.find((x) => M.events[x].cat !== "negative_control") || s.events[0];
      select(first);
    });
    map.on("click", "nosite", (ev) => select(ev.features[0].properties.e));
    const tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
    ["sites", "nosite"].forEach((id) => {
      map.on("mouseenter", id, (ev) => { map.getCanvas().style.cursor = "pointer"; tip.setLngLat(ev.lngLat).setHTML(esc(ev.features[0].properties.name)).addTo(map); });
      map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; tip.remove(); });
    });
    map.on("mouseenter", "dets", (ev) => {
      const p = ev.features[0].properties;
      tip.setLngLat(ev.lngLat).setHTML(`${esc(p.l)} · ${p.h >= 0 ? "+" : ""}${Number(p.h).toFixed(1)} h · ${Number(p.frp).toFixed(1)} MW<br><span class="muted">${esc(p.sat)} ${esc(p.utc)}</span>`).addTo(map);
    });
    map.on("mouseleave", "dets", () => tip.remove());

    const panel = $("#map-panel"), wrap = $("#map-panel-wrap");
    wrap.hidden = false;
    wrap.open = !matchMedia("(max-width: 560px)").matches;   // collapsed on phones so it doesn't cover the map
    panel.addEventListener("change", async (e) => {
      const t = e.target;
      if (t.name === "base") map.setStyle(await BASES[t.value](), { diff: false });
      if (t.name === "layer") { vis[t.value] = t.checked; applyVisibility(); }
    });
    map.once("load", () => {
      const m = location.hash.match(/^#map=(E\d+)/);
      if (m) { $("#map-section").scrollIntoView(); select(m[1]); }
    });
  }

  // table "Map" buttons
  $$(".to-map").forEach((b) => {
    b.hidden = false;
    b.addEventListener("click", () => {
      $("#map-section").scrollIntoView({ behavior: "smooth" });
      if (M) select(b.dataset.event);
      history.replaceState(null, "", "#map=" + b.dataset.event);
    });
  });

  function start() {
    if (!window.maplibregl) {
      mapEl.innerHTML = '<p class="map-fallback">The map library did not load. Every event is listed in the table above, with its own page.</p>';
      return;
    }
    buildMap().catch((err) => { console.error(err); mapEl.innerHTML = '<p class="map-fallback">The map could not be loaded. Every event is in the table above.</p>'; });
  }
  if (window.maplibregl) start(); else window.addEventListener("load", start);
})();
