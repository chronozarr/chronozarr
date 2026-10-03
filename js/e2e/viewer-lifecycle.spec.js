import { expect, test } from './fixtures.js';

// A live viewer intentionally keeps its programs and workers. Replacing stores
// must release raster arrays and keep GPU allocations bounded across dtypes.
test('repeated viewer store replacement releases closed caches and keeps GPU handles bounded', async ({ page, servers, storeUrl }) => {
  await page.addInitScript(() => {
    window.glHandles = new Map();
    for (const kind of ['Texture', 'Program', 'Shader']) {
      const handles = new Set(); window.glHandles.set(kind, handles);
      const proto = WebGL2RenderingContext.prototype;
      const create = proto[`create${kind}`]; const remove = proto[`delete${kind}`];
      proto[`create${kind}`] = function(...args) { const handle = create.apply(this, args); if (handle) handles.add(handle); return handle; };
      proto[`delete${kind}`] = function(handle) { handles.delete(handle); return remove.call(this, handle); };
    }
  });
  const urls = [await storeUrl('u16_mask'), await storeUrl('f32_band')];
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(urls[0])}`);
  await page.waitForFunction(() => window.chronozarr?.viewer.paintedT >= 0 && window.chronozarr.viewer.renderNow().complete);
  const result = await page.evaluate(async urls => {
    const viewer = window.chronozarr.viewer;
    const stores = [];
    const samples = [];
    const baseline = {};
    for (let cycle = 0; cycle < 20; cycle++) {
      const previous = viewer.store;
      await viewer.loadStore(urls[cycle % 2]);
      stores.push(previous);
      const counts = Object.fromEntries([...window.glHandles].map(([kind, handles]) => [kind, handles.size]));
      if (cycle === 2 || cycle === 3) baseline[cycle % 2] = counts;
      if (cycle >= 4) samples.push({ kind: cycle % 2, counts });
    }
    window.retainedClosedStores = stores;
    return { baseline, samples, closedBytes: stores.map(store => store.cacheInfo().bytes + store.cacheInfo().compressedBytes) };
  }, urls);
  expect(result.closedBytes).toEqual(new Array(20).fill(0));
  for (const sample of result.samples) expect(sample.counts).toEqual(result.baseline[sample.kind]);
});
