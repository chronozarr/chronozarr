import assert from 'node:assert/strict';
import path from 'node:path';
import { chromium } from '../../js/node_modules/playwright/index.mjs';
import { startStaticServer } from '../../js/support/static-server.js';
const root = path.resolve(import.meta.dirname, '../..');
const server = await startStaticServer(root);
const browser = await chromium.launch({args:['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
try {
  const page = await browser.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const url = `${server.url}/data/stores/nisar/local-20261002`;
  await page.goto(`${server.url}/js/maplibre/index.html?store=${encodeURIComponent(url)}&p=band`);
  await page.waitForFunction(() => window.chronozarrDemo?.layer.store);
  const result = await page.evaluate(async url => {
    const {createChronozarrPlugin} = await import('/js/geolibre/plugin.js');
    const map = window.chronozarrDemo.map;
    const records = new Map();
    const app = {getMap: () => map, registerExternalNativeLayer: r => records.set(r.id,r), unregisterExternalNativeLayer: id => records.delete(id)};
    const plugin = createChronozarrPlugin({url,product:'band',range:[-25,0],name:'GeoLibre NISAR'});
    if (!plugin.activate(app)) throw Error('Activation failed');
    await plugin.ready;
    const record = records.get(plugin.id);
    if (record.paintMode !== 'plugin') throw Error('Incorrect paint ownership');
    record.paintBridge.setOpacity(0.4);
    const slider = document.querySelector('[aria-label="GeoLibre NISAR timestep"]');
    slider.value = '1'; slider.dispatchEvent(new Event('input'));
    const date = slider.parentElement.textContent;
    const layer = map.getLayer(plugin.id);
    if ((layer.implementation ?? layer).t !== 1) throw Error('Timestep did not change');
    plugin.deactivate();
    if (map.getLayer(plugin.id) || records.size || document.contains(slider)) throw Error('Cleanup failed');
    return date;
  }, url);
  assert.match(result,/2026-08-21/); assert.deepEqual(errors,[]);
  console.log('GeoLibre plugin contract on real MapLibre: render/open, host registration, opacity bridge, timestep and cleanup passed.');
} finally {await browser.close();await server.close();}
