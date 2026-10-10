import assert from 'node:assert/strict';
import { test } from 'node:test';
import proj4 from 'proj4';
import { AOI, VIEWPORT, VIEWS } from './config.mjs';
import { cssPxPerMetreAt, footprintLonLat, mapZoomFor, resolveView, tileOf, utmOfPixel, viewerSearch } from './view.mjs';

test('the zoom view shows four level-0 cells of 512 px and no others', () => {
  const view = VIEWS.zoom;
  const halfWidth = VIEWPORT.width / view.cssPxPerL0 / 2;
  const halfHeight = VIEWPORT.height / view.cssPxPerL0 / 2;
  const [col, row] = view.centerPx;
  const columns = new Set([Math.floor((col - halfWidth) / 512), Math.floor((col + halfWidth) / 512)]);
  const rows = new Set([Math.floor((row - halfHeight) / 512), Math.floor((row + halfHeight) / 512)]);
  assert.deepEqual([...columns], [2, 3]);
  assert.deepEqual([...rows], [2, 3]);
});

test('the overview fits the whole footprint into the window height', () => {
  const view = VIEWS.overview;
  assert.ok(AOI.height * view.cssPxPerL0 <= VIEWPORT.height);
  assert.ok(AOI.width * view.cssPxPerL0 <= VIEWPORT.width);
});

test('MapLibre zoom and CSS pixels per metre are inverse', () => {
  const latitude = -7.7;
  const zoom = mapZoomFor(0.0311, latitude);
  assert.ok(Math.abs(cssPxPerMetreAt(zoom, latitude) - 0.0311) < 1e-12);
});

test('at the overview zoom the Web Mercator footprint is as tall on screen as the viewer draws it', () => {
  const view = resolveView(VIEWS.overview);
  const mercatorY = (lat) => (0.5 - Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)) / (2 * Math.PI)) * 512 * 2 ** view.mapZoom;
  const [, yMin, , yMax] = AOI.bounds;
  const [centreX] = view.centerUtm;
  const south = proj4(AOI.crs, 'EPSG:4326', [centreX, yMin]);
  const north = proj4(AOI.crs, 'EPSG:4326', [centreX, yMax]);
  const mercatorHeight = mercatorY(south[1]) - mercatorY(north[1]);
  const viewerHeight = AOI.height * VIEWS.overview.cssPxPerL0;
  assert.ok(Math.abs(mercatorHeight / viewerHeight - 1) < 0.01, `${mercatorHeight} against ${viewerHeight}`);
});

test('the viewer permalink carries timestep, zoom and projected centre', () => {
  const view = resolveView(VIEWS.zoom);
  assert.equal(viewerSearch(view, 10), '?t=10&z=1.45&c=501010,9154520');
});

test('level-0 pixel corners and centres in UTM', () => {
  assert.deepEqual(utmOfPixel(0, 0), [485650, 9169880]);
  assert.deepEqual(utmOfPixel(1, 2, { centre: true }), [485665, 9169855]);
});

test('the footprint box contains the footprint corners', () => {
  const [west, south, east, north] = footprintLonLat();
  const [lon, lat] = proj4(AOI.crs, 'EPSG:4326', [499445, 9156055]);
  assert.ok(west < lon && lon < east && south < lat && lat < north);
});

test('slippy-map tile numbers', () => {
  assert.deepEqual(tileOf(-75, -7.6, 1), { z: 1, x: 0, y: 1 });
  assert.deepEqual(tileOf(0, 0, 0), { z: 0, x: 0, y: 0 });
});
