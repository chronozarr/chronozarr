import { DAYMET_LCC, MODIS_SINUSOIDAL } from '../support/crs-fixtures.js';
import { expect, test } from './fixtures.js';
import { storedValue } from './stores.js';

// The demo page loads MapLibre GL JS from jsdelivr (pinned in its import map); that is the one request allowed to leave
// the machine, and the basemap style is replaced by an empty one so nothing else is needed.
const EMPTY_STYLE = { version: 8, sources: {}, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#1b2233' } }] };

test('MapLibre demo: the layer opens a synthetic store, reports ready, follows time and product, and reads values under a click', async ({ page, servers, storeUrl }) => {
  const proj4Requests = [];
  page.on('request', (request) => {
    if (request.url().includes('/vendor/proj4/')) proj4Requests.push(request.url());
  });
  await page.route(/^https:\/\/cdn\.jsdelivr\.net\//, (route) => route.continue());
  await page.route('https://demotiles.maplibre.org/style.json', (route) => route.fulfill({ json: EMPTY_STYLE }));
  const storeName = 'u16_sharded';
  await page.goto(`${servers.appUrl}/maplibre/index.html?store=${encodeURIComponent(await storeUrl(storeName))}`);

  const status = page.locator('#status');
  await expect(status).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
  await expect(page.locator('#error')).toBeEmpty();
  const opened = await page.evaluate(() => {
    const { layer, map } = window.chronozarrDemo;
    return { times: layer.times.length, products: layer.products.filter((p) => p.available).map((p) => p.id), zoom: map.getZoom() };
  });
  expect(opened.times).toBe(6);
  expect(opened.products).toEqual(['true_color', 'false_color', 'ndvi', 'ndwi', 'water', 'band']);
  await expect(page.locator('#products button')).toHaveCount(5);
  await expect(page.locator('#time')).toHaveAttribute('max', '5');

  // A product and a timestep each make the layer load and settle again.
  await page.locator('#products button', { hasText: 'NDVI' }).click();
  await expect(page.locator('#products button[aria-pressed="true"]')).toHaveText('NDVI');
  await expect(status).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
  await page.locator('#time').fill('3');
  await expect(page.locator('#time-label')).toHaveText('2024-01-04');
  await expect(status).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
  expect(await page.evaluate(() => window.chronozarrDemo.layer.t)).toBe(3);

  // The map was fitted to the store, so the middle of the map is inside it: the readout is the store's values there.
  const box = await page.locator('#map').boundingBox();
  await page.mouse.click(box.x + box.width / 2 + 150, box.y + box.height / 2);
  const readout = page.locator('#readout table');
  await expect(readout).toBeVisible();
  const rows = Object.fromEntries(await readout.locator('tr').evaluateAll((trs) => trs.map((tr) => [tr.cells[0].textContent.trim(), tr.cells[1].textContent.trim()])));
  const [, col, row] = /col (\d+), row (\d+)/.exec(rows.pixel);
  expect(rows.time).toBe('2024-01-04');
  for (const [band, name] of ['B02', 'B03', 'B04', 'B08'].entries()) {
    expect(rows[name], `${name} under the click at col ${col}, row ${row}`).toMatch(new RegExp(`^${storedValue(storeName, 3, band, Number(row), Number(col))}\\s`));
  }
  expect(proj4Requests, 'a UTM store fetches no proj4').toEqual([]);
});

// A store with no EPSG code is placed from the proj:wkt2 its arrays carry. The expected numbers are pyproj's
// (support/crs-fixtures.js): the outline of the 200 x 200 grid and the lon/lat of four pixel centres.
for (const [storeName, label, fixture] of [['u16_lcc', 'Daymet LCC', DAYMET_LCC], ['u16_sinusoidal', 'MODIS sinusoidal', MODIS_SINUSOIDAL]]) {
  test(`MapLibre demo: a ${label} store with no EPSG code is placed by pyproj's coordinates, reads the stored values there, and is the only one to load proj4`, async ({ page, servers, storeUrl }) => {
    const proj4Requests = [];
    page.on('request', (request) => {
      if (request.url().includes('/vendor/proj4/')) proj4Requests.push(request.url());
    });
    await page.route(/^https:\/\/cdn\.jsdelivr\.net\//, (route) => route.continue());
    await page.route('https://demotiles.maplibre.org/style.json', (route) => route.fulfill({ json: EMPTY_STYLE }));
    await page.goto(`${servers.appUrl}/maplibre/index.html?store=${encodeURIComponent(await storeUrl(storeName))}`);
    await expect(page.locator('#status')).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
    await expect(page.locator('#error')).toBeEmpty();
    expect(proj4Requests, 'proj4 is fetched once, for this store').toEqual([`${servers.appUrl}/vendor/proj4/proj4.js`]);

    const { bounds, crs, wkt2 } = await page.evaluate(() => ({ bounds: window.chronozarrDemo.layer.bounds, crs: window.chronozarrDemo.layer.store.crs, wkt2: window.chronozarrDemo.layer.store.wkt2 }));
    expect(crs).toBeNull();
    expect(wkt2).toBe(fixture.wkt2);
    const [west, south, east, north] = fixture.grid.outline;
    expect(bounds[0][0]).toBeCloseTo(west, 7);
    expect(bounds[0][1]).toBeCloseTo(south, 7);
    expect(bounds[1][0]).toBeCloseTo(east, 7);
    expect(bounds[1][1]).toBeCloseTo(north, 7);

    for (const [col, row, lon, lat] of fixture.grid.centres) {
      const value = await page.evaluate(([lng, lat]) => window.chronozarrDemo.layer.getValueAt({ lng, lat }, { t: 2 }), [lon, lat]);
      expect(value, `pixel centre (${col}, ${row})`).not.toBeNull();
      expect([value.col, value.row]).toEqual([col, row]);
      expect(value.lngLat.lng).toBeCloseTo(lon, 8);
      expect(value.lngLat.lat).toBeCloseTo(lat, 8);
      for (const [band, bandValue] of value.bands.entries()) expect(bandValue.stored, `band ${band} at (${col}, ${row})`).toBe(storedValue(storeName, 2, band, row, col));
    }
    // Half a pixel outside the grid is outside the store.
    const [, , lonOut, latOut] = fixture.grid.centres[0];
    expect(await page.evaluate(([lng, lat]) => window.chronozarrDemo.layer.getValueAt({ lng, lat }), [lonOut - 5, latOut])).toBeNull();

    // The raster is drawn where pyproj puts it: the middle of the footprint is painted, and the map beyond its western edge is not.
    const painted = await page.evaluate(
      ([lon, lat, outLon]) =>
        new Promise((resolve) => {
          const { map } = window.chronozarrDemo;
          map.once('render', () => {
            const canvas = map.getCanvas();
            const gl = canvas.getContext('webgl2');
            const colorAt = ([lng, lat]) => {
              const { x, y } = map.project([lng, lat]);
              const pixel = new Uint8Array(4);
              gl.readPixels(Math.round(x * (canvas.width / canvas.clientWidth)), Math.round(canvas.height - y * (canvas.height / canvas.clientHeight)), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
              return [...pixel.slice(0, 3)];
            };
            resolve({ inside: colorAt([lon, lat]), outside: colorAt([outLon, lat]) });
          });
          map.triggerRepaint();
        }),
      [fixture.grid.centres[1][2], fixture.grid.centres[1][3], fixture.grid.outline[0] - 0.05 * (fixture.grid.outline[2] - fixture.grid.outline[0])],
    );
    const BACKGROUND = [0x1b, 0x22, 0x33];
    expect(painted.outside, 'beyond the footprint is the map background').toEqual(BACKGROUND);
    expect(painted.inside, 'inside the footprint is not').not.toEqual(BACKGROUND);
  });
}

test('MapLibre demo: a SAR store with HH and HV offers the dual-pol composite and draws it in color', async ({ page, servers, storeUrl }) => {
  await page.route(/^https:\/\/cdn\.jsdelivr\.net\//, (route) => route.continue());
  await page.route('https://demotiles.maplibre.org/style.json', (route) => route.fulfill({ json: EMPTY_STYLE }));
  await page.goto(`${servers.appUrl}/maplibre/index.html?store=${encodeURIComponent(await storeUrl('u16_sar'))}&p=dual_pol`);
  await expect(page.locator('#status')).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
  await expect(page.locator('#error')).toBeEmpty();
  expect(await page.evaluate(() => window.chronozarrDemo.layer.products.filter((p) => p.available).map((p) => p.id))).toEqual(['band', 'dual_pol']);
  await expect(page.locator('#products button[aria-pressed="true"]')).toHaveText('Dual-pol composite');

  // Read the drawing buffer inside a render event, where it still holds the frame: the middle of the map is inside
  // the store, where HH, HV and their ratio get three different stretches, so some pixel there is not gray.
  const spread = await page.evaluate(
    () =>
      new Promise((resolve) => {
        const { map } = window.chronozarrDemo;
        map.once('render', () => {
          const canvas = map.getCanvas();
          const gl = canvas.getContext('webgl2');
          const side = 40;
          const pixels = new Uint8Array(side * side * 4);
          gl.readPixels(Math.floor(canvas.width / 2 - side / 2), Math.floor(canvas.height / 2 - side / 2), side, side, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
          let widest = 0;
          for (let i = 0; i < pixels.length; i += 4) widest = Math.max(widest, Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) - Math.min(pixels[i], pixels[i + 1], pixels[i + 2]));
          resolve(widest);
        });
        map.triggerRepaint();
      }),
  );
  expect(spread, 'widest gap between a pixel\'s red, green and blue').toBeGreaterThan(40);
});

// Keep the host map/context alive: deleting a whole canvas can hide custom-layer leaks.
test('repeated layer removal releases cache and GPU handles while reusing decode workers', async ({ page, servers, storeUrl }) => {
  test.setTimeout(90_000);
  await page.addInitScript(() => {
    const NativeWorker = window.Worker;
    window.liveDecodeWorkers = new Set();
    window.workersStarted = 0;
    window.Worker = class extends NativeWorker {
      constructor(...args) {
        super(...args);
        // MapLibre has its own worker while the host map stays alive.
        if (String(args[0]).includes('/chronozarr/decode-worker.js')) {
          window.liveDecodeWorkers.add(this); window.workersStarted++;
        }
      }
      terminate() { window.liveDecodeWorkers.delete(this); return super.terminate(); }
    };
    window.glHandles = new Map();
    for (const kind of ['Texture', 'Buffer', 'VertexArray', 'Program', 'Shader']) {
      const handles = new Set(); window.glHandles.set(kind, handles);
      const proto = WebGL2RenderingContext.prototype;
      const create = proto[`create${kind}`]; const remove = proto[`delete${kind}`];
      proto[`create${kind}`] = function(...args) { const handle = create.apply(this, args); if (handle) handles.add(handle); return handle; };
      proto[`delete${kind}`] = function(handle) { handles.delete(handle); return remove.call(this, handle); };
    }
  });
  await page.route(/^https:\/\/cdn\.jsdelivr\.net\//, route => route.continue());
  await page.route('https://demotiles.maplibre.org/style.json', route => route.fulfill({ json: EMPTY_STYLE }));
  const url = await storeUrl('u16_mask');
  await page.goto(`${servers.appUrl}/maplibre/index.html?store=${encodeURIComponent(url)}`);
  await expect(page.locator('#status')).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
  const result = await page.evaluate(async url => {
    const { ChronozarrLayer } = await import('/maplibre/layer.js');
    const { map, layer: original } = window.chronozarrDemo;
    map.removeLayer(original.id);
    const retained = [];
    const resources = () => Object.fromEntries([...window.glHandles].map(([kind, handles]) => [kind, handles.size]));
    let baseline;
    let firstWorkers;
    const samples = [];
    for (let cycle = 0; cycle < 25; cycle++) {
      const layer = new ChronozarrLayer({ id: `cycle-${cycle}`, url, product: 'true_color', gpuBudgetBytes: 2 * 1024 * 1024, storeOptions: { workers: 2 } });
      await new Promise((resolve, reject) => {
        layer.on('ready', resolve); layer.on('error', event => reject(event.error)); map.addLayer(layer);
      });
      if (cycle === 0) firstWorkers = window.workersStarted;
      const store = layer.store;
      if (store.cacheInfo().bytes <= 0) throw new Error('Expected a decoded frame before removing the layer');
      map.removeLayer(layer.id);
      retained.push(store);
      const sample = resources();
      if (cycle === 0) baseline = sample;
      samples.push(sample);
    }
    // Deliberately retain the closed stores: zero bytes must follow explicit cleanup, not GC luck.
    window.retainedClosedStores = retained;
    return { baseline, samples, firstWorkers, workersStarted: window.workersStarted, cacheBytes: retained.map(store => store.cacheInfo().bytes + store.cacheInfo().compressedBytes) };
  }, url);
  for (const sample of result.samples) expect(sample).toEqual(result.baseline);
  expect(result.firstWorkers).toBeGreaterThanOrEqual(2);
  expect(result.workersStarted).toBe(result.firstWorkers);
  expect(result.cacheBytes).toEqual(new Array(25).fill(0));
  await expect.poll(() => page.evaluate(() => window.liveDecodeWorkers.size), { timeout: 35_000 }).toBe(0);
  expect(await page.evaluate(() => window.chronozarrDemo.map.getCanvas().getContext('webgl2').isContextLost())).toBe(false);
});
