import { expect, test } from './fixtures.js';

// js/geolibre/plugin.js adapts the MapLibre layer to the GeoLibre plugin API. GeoLibre is not a dependency, so the
// host is a stand-in with the three methods the plugin calls, on the real MapLibre map of the demo page. As in the
// MapLibre spec, MapLibre GL JS comes from the pinned CDN and the basemap style is replaced by an empty one.
const EMPTY_STYLE = { version: 8, sources: {}, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#1b2233' } }] };

test('GeoLibre plugin: adds the layer to the host map, registers it, follows the time slider and the opacity bridge, and removes everything on deactivate', async ({ page, servers, storeUrl }) => {
  await page.route(/^https:\/\/cdn\.jsdelivr\.net\//, (route) => route.continue());
  await page.route('https://demotiles.maplibre.org/style.json', (route) => route.fulfill({ json: EMPTY_STYLE }));
  const url = await storeUrl('u16_sharded');
  await page.goto(`${servers.appUrl}/maplibre/index.html?store=${encodeURIComponent(url)}`);
  await expect(page.locator('#status')).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });

  const activated = await page.evaluate(async (storeUrl) => {
    const { createChronozarrPlugin } = await import('/geolibre/plugin.js');
    const { map } = window.chronozarrDemo;
    const records = new Map();
    const host = {
      getMap: () => map,
      registerExternalNativeLayer: (record) => records.set(record.id, record),
      unregisterExternalNativeLayer: (id) => records.delete(id),
    };
    const plugin = createChronozarrPlugin({ url: storeUrl, product: 'band', range: [0, 1], name: 'GeoLibre test' });
    window.geolibre = { map, records, plugin };
    return plugin.activate(host);
  }, url);
  expect(activated).toBe(true);

  // The host learns of the layer once the store is open, as a layer the plugin paints itself.
  await page.evaluate(() => window.geolibre.plugin.ready);
  const registered = await page.evaluate(() => {
    const { plugin, records } = window.geolibre;
    const record = records.get(plugin.id);
    return record && { ids: [...records.keys()], paintMode: record.paintMode, nativeLayerIds: record.nativeLayerIds, source: record.source, hasBounds: Array.isArray(record.metadata?.bounds) };
  });
  expect(registered).toEqual({ ids: ['chronozarr-geolibre'], paintMode: 'plugin', nativeLayerIds: ['chronozarr-geolibre'], source: { type: 'chronozarr', url }, hasBounds: true });

  // The slider covers the six timesteps of the store, and moving it moves the layer and the label.
  const slider = page.getByLabel('GeoLibre test timestep');
  await expect(slider).toBeEnabled();
  await expect(slider).toHaveAttribute('max', '5');
  await slider.fill('1');
  await expect(slider.locator('xpath=..')).toContainText('GeoLibre test: 2024-01-02');
  const layerState = () => page.evaluate(() => {
    const { map, plugin } = window.geolibre;
    const registered = map.getLayer(plugin.id);
    const layer = registered?.implementation ?? registered;
    return layer && { t: layer.t, opacity: layer.opacity };
  });
  expect(await layerState()).toEqual({ t: 1, opacity: 1 });

  // The opacity bridge is what the host calls when its own layer panel changes the opacity.
  await page.evaluate(() => window.geolibre.records.get(window.geolibre.plugin.id).paintBridge.setOpacity(0.4));
  expect(await layerState()).toEqual({ t: 1, opacity: 0.4 });

  // Deactivate takes the layer, the registration and the control off the host.
  await page.evaluate(() => window.geolibre.plugin.deactivate());
  expect(await layerState()).toBeFalsy();
  expect(await page.evaluate(() => window.geolibre.records.size)).toBe(0);
  await expect(slider).toHaveCount(0);
});
