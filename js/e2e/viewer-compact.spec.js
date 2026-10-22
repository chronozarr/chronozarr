import { expect, test } from './fixtures.js';
import { captureFrame, expectFrameMatchesStore, openViewer, waitForPaintedTime } from './viewer-helpers.js';

test('compact GPU reads match raw rendering across bit widths, boundaries, masks and bands', async ({ page, servers }) => {
  await page.route('**/compact-test.html', (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><link rel="icon" href="data:,">' }));
  await page.goto(`${servers.appUrl}/compact-test.html`);
  const results = await page.evaluate(async () => {
    const { Renderer } = await import('/demo/renderer.js');
    const { packUint16 } = await import('/chronozarr/compact.js');
    const width = 33, height = 73, bands = 3;
    const elements = width * height * bands, blockCount = Math.ceil(elements / 128);
    const blockWidth = Math.min(256, blockCount);
    const slotBytes = width * Math.ceil(Math.ceil(elements / 2) / width) * 4
      + blockWidth * Math.ceil(blockCount / blockWidth) * 8 + width * height;
    const budgetRenderer = new Renderer(document.createElement('canvas'));
    const budget = slotBytes * 3 + slotBytes - 1;
    const slots = budgetRenderer.planSlots(bands, width, height, budget, 100, 2, true, true);
    if (slots !== 3) throw new Error(`Odd-shape compact budget planned ${slots} slots instead of 3`);
    budgetRenderer.configure({ nBand: bands, chunkWidth: width, chunkHeight: height, slots, hasMask: true, compact: true });
    if (budgetRenderer.allocatedBytes !== slots * slotBytes || budgetRenderer.allocatedBytes > budget) throw new Error('Actual compact GPU allocation exceeds its planned budget');
    budgetRenderer.dispose();
    const render = (data, compact, mask, band, range) => {
      const canvas = document.createElement('canvas');
      canvas.width = width; canvas.height = height;
      const renderer = new Renderer(canvas);
      renderer.configure({ nBand: bands, chunkWidth: width, chunkHeight: height, slots: 1, hasMask: !!mask, compact });
      const slot = renderer.upload('test', { lod: 0, row: 0, col: 0, t: 0 }, data);
      if (mask) renderer.uploadMask(slot, mask);
      renderer.beginPaint({ width, height, cx: width / 2, cy: height / 2, scale: 1, shader: 5,
        inputs: [band, -1, -1], stretchLo: 0, display: 'linear', range, nodata: null,
        unitScale: [1, 1, 1], unitDivisor: [0, 0, 0], unitOffset: [0, 0, 0] }, { clear: true });
      renderer.drawCell({ x: 0, y: 0, w: width, h: height }, { w: width, h: height }, slot, mask ? slot : -1);
      const rgba = renderer.readFrame(width, height);
      renderer.dispose();
      return rgba;
    };
    const compare = (raw, packed, mask, label) => {
      for (let band = 0; band < bands; band++) {
        for (const range of [[0, 65535], [3000, 3255]]) {
          const a = render(raw, false, mask, band, range), b = render(packed, true, mask, band, range);
          if (new Set(a.filter((_, i) => i % 4 === 0)).size < 2) throw new Error(`${label}: reference image is uniform`);
          for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) throw new Error(`${label} band ${band} range ${range}: RGBA byte ${i}: ${a[i]} != ${b[i]}`);
        }
      }
    };
    const raw = new Uint16Array(width * height * bands);
    const widths = new Set();
    for (let start = 0, block = 0; start < raw.length; start += 128, block++) {
      const bits = block % 17, max = 2 ** bits - 1, min = bits === 16 ? 0 : 3000;
      widths.add(bits);
      for (let i = start; i < Math.min(start + 128, raw.length); i++) raw[i] = min + ((i - start) * 997 % (max + 1));
      raw[start] = min;
      raw[Math.min(start + 127, raw.length - 1)] = min + max;
    }
    const packed = packUint16(raw);
    if (!packed) throw new Error('Mixed-width fixture should compact');
    const mask = Uint8Array.from({ length: width * height }, (_, i) => i % 7 ? 1 : 0);
    compare(raw, packed, null, 'packed');
    compare(raw, packed, mask, 'masked packed');
    const noise = Uint16Array.from({ length: raw.length }, (_, i) => i % 2 ? 65535 : 0);
    if (packUint16(noise)) throw new Error('Full-width fixture should retain raw fallback');
    compare(noise, noise, mask, 'raw fallback');
    const constant = new Uint16Array(raw.length).fill(65535), constantPacked = packUint16(constant);
    if (constantPacked.words.length !== 0) throw new Error('Constant fixture should have zero packed words');
    const a = render(constant, false, mask, 2, [0, 65535]);
    const b = render(constantPacked, true, mask, 2, [0, 65535]);
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) throw new Error(`Zero-word constant mismatch at RGBA byte ${i}`);
    return { widths: [...widths], length: raw.length };
  });
  expect(results.widths).toEqual(Array.from({ length: 17 }, (_, i) => i));
  expect(results.length % 128).not.toBe(0);
});

test('compact worker reader preserves CPU values and real viewer masked frames', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('u16_mask');
  await openViewer(page, servers, url);
  const facts = await page.evaluate(async (url) => {
    const { openStore } = await import('/chronozarr/decoder.js');
    const golden = await openStore(url, { workers: 0 });
    const expected = new Map();
    for (const t of [0, 1]) for (const row of [0, 1]) for (const col of [0, 1]) {
      expected.set(`${t}/${row}/${col}`, (await golden.getRaw(0, row, col, t)).slice());
    }
    golden.close();
    let readyCount = 0, packedMessages = 0, resolveReady;
    const ready = new Promise((resolve) => { resolveReady = resolve; });
    const store = await openStore(url, { workers: 2, spawnWorker: () => {
      const worker = new Worker('/chronozarr/decode-worker.js', { type: 'module' });
      worker.addEventListener('message', ({ data }) => {
        if (data.type === 'ready' && ++readyCount === 2) resolveReady();
        if (data.type === 'decoded' && data.data?.words) packedMessages++;
      });
      return worker;
    } });
    await ready;
    store.enableCompactCache();
    for (const t of [0, 1]) for (const row of [0, 1]) for (const col of [0, 1]) {
      const ensured = await store.ensureRaw(0, row, col, t);
      if (ensured !== undefined || !store.hasCachedRaw(0, row, col, t)) throw new Error('ensureRaw did not retain its chunk without returning CPU data');
      const packed = store.peekCompact(0, row, col, t);
      if (!packed) throw new Error('Synthetic chunk did not retain GPU compact backing');
      const raw = await store.getRaw(0, row, col, t), cell = await store.getCell(0, row, col, t);
      const want = expected.get(`${t}/${row}/${col}`);
      if (!(raw instanceof Uint16Array) || !(cell.data instanceof Uint16Array)) throw new Error('CPU APIs stopped returning uint16 arrays');
      for (let i = 0; i < want.length; i++) if (want[i] !== raw[i] || want[i] !== cell.data[i]) throw new Error(`CPU values changed at ${t}/${row}/${col}/${i}`);
      if (store.peekCompact(0, row, col, t) !== packed) throw new Error('CPU access replaced compact resident backing');
      const sample = store.samplePixel(0, row, col, t, 17, 9), plane = 128 * 128;
      for (let band = 0; band < sample.length; band++) if (sample[band] !== want[band * plane + 9 * 128 + 17]) throw new Error('Pixel inspector values changed');
    }
    await window.chronozarr.viewer.loadStore(url, { store, lod: 0 });
    return { readyCount, packedMessages, compact: store.compactCacheEnabled };
  }, url);
  expect(facts.readyCount).toBe(2);
  expect(facts.packedMessages).toBeGreaterThanOrEqual(8);
  expect(facts.compact).toBe(true);
  await waitForPaintedTime(page, 0);
  expectFrameMatchesStore(await captureFrame(page), 'u16_mask', 0);
  await page.keyboard.press('ArrowRight');
  await waitForPaintedTime(page, 1);
  expectFrameMatchesStore(await captureFrame(page), 'u16_mask', 1);
});
