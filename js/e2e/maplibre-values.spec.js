import { expect, test } from './fixtures.js';

// Exercise the real layer and WebGL2 shader without MapLibre or a remote basemap.
test('MapLibre values, automatic display and packed uploads honor masks in a shared GL context', async ({ page, servers }) => {
  await page.route('**/maplibre-values-probe.html', (route) => route.fulfill({ contentType: 'text/html', body: '<canvas width="256" height="256"></canvas>' }));
  await page.goto(`${servers.appUrl}/maplibre-values-probe.html`);
  const result = await page.evaluate(async () => {
    const { ChronozarrLayer } = await import('/maplibre/layer.js');
    const { buildSyntheticStore, maskValue } = await import('/support/synthetic-store.js');
    const { computeStretchLo, dualPolRanges } = await import('/shared/products.js');
    const { lonLatToMercator } = await import('/maplibre/projection.js');
    const canvas = document.querySelector('canvas');
    const gl = canvas.getContext('webgl2', { preserveDrawingBuffer: true, antialias: false });
    if (!gl) throw Error('WebGL2 unavailable');
    // The layer shares a GL context and must restore its caller's unpack alignment.
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 8);
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    const reports = [];
    const check = (ok, label) => { if (!ok) throw Error(label); };
    const equal = (actual, expected, label) => check(JSON.stringify(actual) === JSON.stringify(expected), `${label}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
    const map = { triggerRepaint() {}, getLayer() { return false; }, getZoom() { return 10; } };
    async function run(label, spec, options = {}, mutate = null, inspect = null) {
      const memory = buildSyntheticStore({ nTime: 1, nBand: 1, height: 2, width: 2, chunk: 2, sharded: false, crs: 'EPSG:4326', transform: [1, 0, 0, 0, -1, spec.height ?? 2], bandObjects: [{ name: 'value', scale: 1, offset: 0 }], ...spec });
      if (mutate) mutate(memory);
      const layer = new ChronozarrLayer({ url: `memory://${label}`, product: 'band', ...options, storeOptions: { store: memory, workers: 0 } });
      const errors = [];
      layer.on('error', e => errors.push(String(e.error)));
      layer.onAdd(map, gl);
      const store = await layer.opened;
      async function render(t = 0) {
        for (let row = 0; row < store.levels[0].gridRows; row++) for (let col = 0; col < store.levels[0].gridCols; col++) {
          await store.getRaw(0, row, col, t);
          if (store.hasMask) await store.getMask(0, row, col, t);
        }
        layer.setTime(t);
        const [[west, south], [east, north]] = layer.bounds;
        const [xmin, ymax] = lonLatToMercator(west, south), [xmax, ymin] = lonLatToMercator(east, north);
        const sx = 2 / (xmax - xmin), sy = 2 / (ymax - ymin), cx = (xmin + xmax) / 2, cy = (ymin + ymax) / 2;
        const matrix = [sx, 0, 0, 0, 0, -sy, 0, 0, 0, 0, 1, 0, -cx * sx, cy * sy, 0, 1];
        gl.viewport(0, 0, canvas.width, canvas.height); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
        layer.render({ defaultProjectionData: { mainMatrix: matrix } });
        gl.finish();
        const glError = gl.getError(); check(glError === gl.NO_ERROR, `${label} GL error ${glError}`);
        check(gl.getParameter(gl.UNPACK_ALIGNMENT) === 8, `${label} changed shared unpack alignment`);
        const program = gl.getParameter(gl.CURRENT_PROGRAM);
        const uniform = name => { const value = gl.getUniform(program, gl.getUniformLocation(program, name)); return typeof value === 'number' ? value : Array.from(value); };
        const pixel = (col, row) => {
          const x = Math.floor((col + .5) * canvas.width / store.levels[0].width);
          const y = Math.floor((store.levels[0].height - row - .5) * canvas.height / store.levels[0].height);
          const rgba = new Uint8Array(4); gl.readPixels(x, y, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, rgba); return Array.from(rgba);
        };
        return { lo: uniform('u_rangeLo'), hi: uniform('u_rangeHi'), stretch: uniform('u_stretchLo'), pixel };
      }
      try {
        const frame = await render();
        if (inspect) await inspect({ layer, store, frame, render });
        check(errors.length === 0, `${label}: ${JSON.stringify(errors)}`);
        reports.push(label);
      } finally { layer.remove(); }
    }
    await run('linear masked outliers and valid zero', { dtype: 'float32', mask: true, values: (t, b, y, x) => maskValue(t, y, x) ? (x === 1 ? 0 : 12) : 1e6 }, {}, null, async ({ layer, frame }) => {
      equal(frame.lo, [0, 0, 0], 'linear low'); equal(frame.hi, [12, 12, 12], 'linear high');
      equal(frame.pixel(0, 0), [0, 0, 0, 0], 'masked fragment'); equal(frame.pixel(1, 0), [0, 0, 0, 255], 'valid zero fragment'); equal(frame.pixel(0, 1), [255, 255, 255, 255], 'linear white endpoint');
      const masked = await layer.getValueAt([.5, 1.5]), valid = await layer.getValueAt([1.5, 1.5]);
      check(!masked.valid, 'masked inspector validity'); equal(masked.bands.map(b => [b.stored, b.value]), [[null, null]], 'masked inspector values');
      check(valid.valid, 'zero inspector validity'); equal(valid.bands.map(b => [b.stored, b.value]), [[0, 0]], 'valid zero inspector');
    });
    await run('masked NDVI inspection', { nBand: 2, mask: true, bandObjects: [{ name: 'red', scale: 1 }, { name: 'nir', scale: 1 }], values: (t, b, y, x) => x === 1 ? (b === 0 ? 0 : 2) : (b === 0 ? 4 : 8) }, { product: 'ndvi' }, null, async ({ layer, frame }) => {
      const masked = await layer.getValueAt([.5, 1.5]), valid = await layer.getValueAt([1.5, 1.5]);
      check(!masked.valid && masked.ndvi === null && masked.ndwi === null && !masked.isWater, 'masked indices'); equal(masked.bands.map(b => b.stored), [null, null], 'masked bands');
      check(valid.valid && valid.ndvi === 1, 'valid zero NDVI'); equal(frame.pixel(0, 0), [0, 0, 0, 0], 'NDVI mask fragment');
    });
    await run('dual-pol masked automatic channels', { dtype: 'float32', nBand: 2, mask: true, bandObjects: [{ name: 'VV', scale: 1, units: 'dB' }, { name: 'VH', scale: 1, units: 'dB' }], values: (t, b, y, x) => maskValue(t, y, x) ? (b === 0 ? (x === 1 ? 0 : -10) : -21) : 1e6 }, { product: 'dual_pol' }, null, ({ frame }) => {
      const ranges = dualPolRanges([[0, -21], [-10, -21]]);
      equal(frame.lo, ranges.map(r => r[0]), 'dual-pol low'); equal(frame.hi, ranges.map(r => r[1]), 'dual-pol high');
      equal(frame.pixel(1, 0), [255, 0, 255, 255], 'dual-pol magenta'); equal(frame.pixel(0, 0), [0, 0, 0, 0], 'dual-pol mask');
    });
    const rgb = (t, b, y, x) => maskValue(t, y, x) ? 1000 + b * 500 + y * 100 + x : 1;
    await run('reflectance masked automatic stretch', { nBand: 3, mask: true, width: 4, height: 4, chunk: 4, bandObjects: ['red', 'green', 'blue'].map(name => ({ name, scale: .0001, units: 'reflectance' })), values: rgb }, { product: 'true_color' }, null, ({ frame }) => {
      const samples = []; for (let y = 0; y < 4; y++) for (let x = 0; x < 4; x++) if (maskValue(0, y, x)) samples.push([0, 1, 2].map(b => rgb(0, b, y, x) * .0001));
      const expected = computeStretchLo(samples); check(Math.abs(frame.stretch - expected) < 1e-6, `reflectance stretch ${frame.stretch} != ${expected}`);
      equal(frame.pixel(0, 0), [0, 0, 0, 0], 'reflectance mask'); check(frame.pixel(1, 0)[3] === 255, 'reflectance valid fragment');
    });
    await run('partial edge mask stride', { dtype: 'float32', mask: true, height: 3, width: 1, values: (t, b, y) => y === 0 ? 1e6 : 10 + y }, {}, null, ({ frame }) => {
      equal(frame.lo, [11, 11, 11], 'edge low'); equal(frame.hi, [12, 12, 12], 'edge high'); equal(frame.pixel(0, 0), [0, 0, 0, 0], 'edge masked'); equal(frame.pixel(0, 2), [255, 255, 255, 255], 'edge endpoint');
    });
    await run('empty frame waits for linear range', { dtype: 'float32', mask: true, nTime: 2, values: (t, b, y, x) => 10 + 10 * t + y + x }, {}, memory => memory.files.get('/0/mask/c/0/0/0').fill(0), async ({ frame, render }) => {
      equal(frame.lo, [0, 0, 0], 'empty low'); equal(frame.hi, [1, 1, 1], 'empty high'); equal(frame.pixel(1, 0), [0, 0, 0, 0], 'empty fragment');
      const next = await render(1); equal(next.lo, [20, 20, 20], 'next low'); equal(next.hi, [22, 22, 22], 'next high'); equal(next.pixel(1, 1), [255, 255, 255, 255], 'next endpoint');
    });
    await run('empty frame waits for reflectance stretch', { nTime: 2, nBand: 3, mask: true, width: 4, height: 4, chunk: 4, bandObjects: ['red', 'green', 'blue'].map(name => ({ name, scale: .0001, units: 'reflectance' })), values: rgb }, { product: 'true_color' }, memory => memory.files.get('/0/mask/c/0/0/0').fill(0), async ({ frame, render }) => {
      check(frame.stretch === 0, 'empty reflectance default'); const next = await render(1); check(next.stretch > .5, 'valid reflectance stretch not measured');
    });
    for (const [dtype, nBand, chunk] of [['uint8', 2, 1], ['uint8', 1, 2], ['uint8', 1, 3], ['uint16', 1, 3], ['float32', 1, 2], ['float32', 1, 3]]) {
      await run(`packed ${dtype}, ${nBand} bands, chunk ${chunk}`, {
        dtype, nBand, chunk, height: chunk, width: chunk, mask: true,
        bandObjects: Array.from({ length: nBand }, (_, b) => ({ name: `band${b}`, scale: 1 })),
      });
    }
    return { renderer, reports };
  });
  expect(result.reports).toHaveLength(13);
  expect(result.renderer).toBeTruthy();
});
