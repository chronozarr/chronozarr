// The same view for three renderers. The chronozarr viewer draws the store's UTM grid; MapLibre (zarr-layer and the
// raster tiles) draws Web Mercator. A view is fixed by its centre and by how many CSS pixels one ground metre spans on
// screen, which gives the viewer's `z` and `c` permalink parameters and MapLibre's centre and zoom.

import proj4 from 'proj4';
import { AOI } from './config.mjs';

const EQUATOR_M = 40075016.68557849;
const radians = (degrees) => (degrees * Math.PI) / 180;

/** UTM coordinates of the top-left corner of level-0 pixel (col, row), or of its centre when `centre` is set. */
export function utmOfPixel(col, row, { centre = false } = {}) {
  const offset = centre ? 0.5 : 0;
  return [AOI.bounds[0] + (col + offset) * AOI.resolution, AOI.bounds[3] - (row + offset) * AOI.resolution];
}

export const lonLatOfUtm = (x, y) => proj4(AOI.crs, 'EPSG:4326', [x, y]);

/** MapLibre zoom (512 px world at zoom 0) at which one ground metre at latitude `latitude` spans `cssPxPerMetre` CSS pixels. */
export function mapZoomFor(cssPxPerMetre, latitude) {
  return Math.log2((cssPxPerMetre * EQUATOR_M * Math.cos(radians(latitude))) / 512);
}

/** The inverse of mapZoomFor: CSS pixels per ground metre. */
export function cssPxPerMetreAt(mapZoom, latitude) {
  return (512 * 2 ** mapZoom) / (EQUATOR_M * Math.cos(radians(latitude)));
}

/** Everything a driver needs for one named view spec of config.mjs. */
export function resolveView(spec) {
  const [x, y] = utmOfPixel(...spec.centerPx);
  const [lon, lat] = lonLatOfUtm(x, y);
  const cssPxPerMetre = spec.cssPxPerL0 / AOI.resolution;
  return {
    name: spec.name,
    cssPxPerL0: spec.cssPxPerL0,
    centerUtm: [x, y],
    centerLonLat: [lon, lat],
    mapZoom: mapZoomFor(cssPxPerMetre, lat),
    groundMetresPerCssPx: 1 / cssPxPerMetre,
    pin: spec.pin ?? {},
    only: spec.only ?? null,
  };
}

/** The viewer's permalink query for a view at timestep t (js/demo/permalink.js: z = CSS px per level-0 px, c = projected centre). */
export function viewerSearch(view, t) {
  return `?t=${t}&z=${Number(view.cssPxPerL0.toPrecision(6))}&c=${view.centerUtm[0]},${view.centerUtm[1]}`;
}

/** The footprint as WGS84 [west, south, east, north] (the four corners, so that the box contains the rotated footprint). */
export function footprintLonLat() {
  const [xMin, yMin, xMax, yMax] = AOI.bounds;
  const corners = [
    [xMin, yMin],
    [xMax, yMin],
    [xMax, yMax],
    [xMin, yMax],
  ].map(([x, y]) => lonLatOfUtm(x, y));
  const lons = corners.map((c) => c[0]);
  const lats = corners.map((c) => c[1]);
  return [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
}

/** Slippy-map tile containing lon/lat at zoom z (256 px tile scheme, as TiTiler's WebMercatorQuad). */
export function tileOf(lon, lat, z) {
  const n = 2 ** z;
  const x = Math.floor(((lon + 180) / 360) * n);
  const y = Math.floor(((1 - Math.asinh(Math.tan(radians(lat))) / Math.PI) / 2) * n);
  return { z, x, y };
}
