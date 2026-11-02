import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pixelLocation } from '../demo/inspector.js';
import { DAYMET_LCC, MODIS_SINUSOIDAL } from '../support/crs-fixtures.js';

test('pixel coordinates refer to the pixel centre and use latitude, longitude order', async () => {
  assert.deepEqual(await pixelLocation({ x: 2, y: 3 }, {
    crs: 'EPSG:4326', transform: [1, 0, 10, 0, -1, 20],
  }), { lat: 16.5, lon: 12.5 });
  assert.deepEqual(await pixelLocation({ x: 0, y: 0 }, {
    crs: 'EPSG:4326', transform: [1, 0, 190, 0, -1, 20],
  }), { lat: 19.5, lon: -169.5 });
});

test('projected pixel coordinates match independent pyproj reference points', async () => {
  const store = { crs: 'EPSG:32718', transform: [10, 0, 486385.2007, 0, -10, 9153922.5519] };
  const { lat, lon } = await pixelLocation({ x: 0, y: 0 }, store);
  assert.ok(Math.abs(lat - -7.6543) < 2e-9);
  assert.ok(Math.abs(lon - -75.1234) < 2e-9);
});

for (const definition of [DAYMET_LCC, MODIS_SINUSOIDAL]) {
  for (const representation of definition.projjson ? ['wkt2', 'projjson'] : ['wkt2']) {
    test(`pixel coordinates read ${representation} without an EPSG code`, async () => {
      const store = { crs: null, [representation]: definition[representation], transform: definition.grid.transform };
      for (const [x, y, lon, lat] of definition.grid.centres) {
        const location = await pixelLocation({ x, y }, store);
        assert.ok(Math.abs(location.lat - lat) < 1e-8);
        assert.ok(Math.abs(location.lon - lon) < 1e-8);
      }
    });
  }
}

test('unavailable projections and invalid coordinates leave the pixel inspector usable', async () => {
  for (const store of [null, {}, { crs: 'EPSG:2154', transform: [1, 0, 0, 0, -1, 0] }, { crs: 'EPSG:4326', transform: [1, 0, 0, 0, -1, 100] }]) {
    assert.equal(await pixelLocation({ x: 0, y: 0 }, store), null);
  }
});
