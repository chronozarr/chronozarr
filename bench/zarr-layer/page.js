// Page-side helpers for the zarr-layer measurements. The Node drivers (probe.mjs, compare.mjs) call these through
// page.evaluate; nothing here decides what is measured.
//
//   zl.createMap({ center, zoom })               a MapLibre map with an empty black style, no interaction
//   zl.addLayer({ source | store, selector })     a ZarrLayer drawing the natural-colour composite of B04/B03/B02
//   zl.layerState(layer)                          how many visible regions of the active level hold the current selector
//   zl.watch(layer)                               records one event per MapLibre render with that state
//   zl.aoiView(bounds, crs, zoom)                 lon/lat of an AOI's centre and the on-screen size of the AOI at a zoom

import * as maplibregl from 'maplibre-gl';
import { ZarrLayer } from '@carbonplan/zarr-layer';
import proj4 from 'proj4';
import * as zarr from 'zarrita';

const RGB_FRAG = `
  if (isnan(B04) || isnan(B03) || isnan(B02)) {
    discard;
  }
  vec3 rgb = clamp(vec3(B04, B03, B02) / 3000.0, 0.0, 1.0);
  rgb = pow(rgb, vec3(1.0 / 2.2));
  fragColor = vec4(rgb * opacity, opacity);
`;

/**
 * Edits applied to the root zarr.json (attributes and the inline consolidated metadata) by `patchedStore`, to test in the
 * browser what a change of the store's attributes would do without touching the published store.
 */
const PATCHES = {
  // Remove pixels_per_tile from every multiscales dataset: zarr-layer reads its presence as "global slippy-map pyramid".
  'no-pixels-per-tile': (doc) => {
    for (const ms of doc.attributes?.multiscales ?? []) for (const d of ms.datasets ?? []) delete d.pixels_per_tile;
  },
};

/** The store at `url` with its root zarr.json edited by the named patch, behind zarrita's consolidated-metadata reader. */
async function patchedStore(url, patchName) {
  const inner = new zarr.FetchStore(url);
  const patch = PATCHES[patchName];
  if (!patch) throw new Error(`unknown patch ${patchName}`);
  const edited = {
    async get(key, options) {
      const bytes = await inner.get(key, options);
      if (key !== '/zarr.json' || !bytes) return bytes;
      const doc = JSON.parse(new TextDecoder().decode(bytes));
      patch(doc);
      return new TextEncoder().encode(JSON.stringify(doc));
    },
    getRange: (key, range, options) => inner.getRange(key, range, options),
  };
  return zarr.withConsolidatedMetadata(edited, { format: 'v3' });
}

const GRAY_COLORMAP = ['#000000', '#ffffff'];

function createMap({ center, zoom }) {
  const map = new maplibregl.Map({
    container: 'map',
    style: { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#000' } }] },
    center,
    zoom,
    interactive: false,
    attributionControl: false,
    fadeDuration: 0,
    canvasContextAttributes: { preserveDrawingBuffer: true },
  });
  window.map = map;
  return new Promise((resolve, reject) => {
    map.once('load', () => resolve(map));
    map.once('error', (event) => reject(event.error ?? new Error('map error')));
  });
}

async function addLayer({ id = 'zarr', source, patch = null, selector, variable = 'data', extra = {} }) {
  const store = patch ? await patchedStore(source, patch) : undefined;
  const layer = new ZarrLayer({
    id,
    source: patch ? undefined : source,
    store,
    variable,
    selector,
    colormap: GRAY_COLORMAP,
    clim: [0, 3000],
    customFrag: RGB_FRAG,
    ...extra,
  });
  window.layer = layer;
  window.map.addLayer(layer);
  return layer;
}

/** The state of the layer's active level against the current selector version, from its own region cache. */
function layerState(layer) {
  const renderer = layer.regionRenderer;
  if (!renderer || !renderer.activeLevel) return { level: null, total: 0, loaded: 0, version: renderer?.selectorVersion ?? null, regions: 0 };
  const level = renderer.activeLevel.index;
  const visible = renderer.lastVisibleRegions ?? [];
  let loaded = 0;
  let loading = 0;
  for (const { regionX, regionY } of visible) {
    const region = renderer.regionCache.get(renderer.makeRegionKey(level, regionX, regionY));
    if (!region) continue;
    if (region.loading) loading++;
    const bands = renderer.rendersFromBandTextures ? renderer.bandNames : null;
    const uploaded = bands ? bands.every((band) => region.bandTexturesUploaded.has(band)) : region.textureUploaded;
    if (!region.loading && region.selectorVersion === renderer.selectorVersion && uploaded) loaded++;
  }
  return { level, total: visible.length, loaded, loading, version: renderer.selectorVersion };
}

/** One event per render: {at, ...layerState}. `events` is the array to push to; call the returned function to stop. */
function watch(layer, events) {
  const onRender = () => events.push({ at: performance.now(), ...layerState(layer) });
  window.map.on('render', onRender);
  return () => window.map.off('render', onRender);
}

/** Resolves with the performance.now() of the first render at which every visible region holds the current selector. */
function whenComplete(layer, { timeoutMs = 120000 } = {}) {
  return new Promise((resolve, reject) => {
    const started = performance.now();
    const check = () => {
      const state = layerState(layer);
      if (state.total > 0 && state.loaded === state.total) {
        window.map.off('render', check);
        resolve({ at: performance.now(), ...state });
      } else if (performance.now() - started > timeoutMs) {
        window.map.off('render', check);
        reject(new Error(`layer not complete after ${timeoutMs} ms: ${JSON.stringify(state)}`));
      } else {
        window.map.triggerRepaint();
      }
    };
    window.map.on('render', check);
    check();
  });
}

/** Centre (lon/lat) of an AOI given in `crs` and its on-screen width and height in CSS pixels at `zoom` (512 px world at zoom 0). */
function aoiView({ bounds, crs, zoom }) {
  const [xMin, yMin, xMax, yMax] = bounds;
  const toLonLat = (x, y) => proj4(crs, 'EPSG:4326', [x, y]);
  const toMercator = (lon, lat) => {
    const x = (lon + 180) / 360;
    const s = Math.sin((lat * Math.PI) / 180);
    const y = 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
    return [x, y];
  };
  const [cx, cy] = [(xMin + xMax) / 2, (yMin + yMax) / 2];
  const center = toLonLat(cx, cy);
  const corners = [toLonLat(xMin, yMin), toLonLat(xMax, yMin), toLonLat(xMax, yMax), toLonLat(xMin, yMax)].map(([lon, lat]) => toMercator(lon, lat));
  const xs = corners.map((c) => c[0]);
  const ys = corners.map((c) => c[1]);
  const world = 512 * 2 ** zoom;
  return { center, widthPx: (Math.max(...xs) - Math.min(...xs)) * world, heightPx: (Math.max(...ys) - Math.min(...ys)) * world };
}

window.zl = { maplibregl, ZarrLayer, proj4, createMap, addLayer, layerState, watch, whenComplete, aoiView };
