import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { build } from 'esbuild';
import { startStaticServer } from '../support/static-server.js';
import { expect, test } from './fixtures.js';
import { COMPRESSORS, compressedStore, writeTree } from './make-node-fixtures.mjs';

test('a split consumer bundle retries the emitted Zstd asset after a 503', async ({ page, consoleErrors }) => {
  const root = await mkdtemp(path.join(tmpdir(), 'chronozarr-reader-bundle-'));
  let server;
  try {
    // Use the package's public export and esbuild's normal split output, without the viewer's build plugins.
    await build({
      stdin: { contents: "export { openStore } from 'chronozarr';", resolveDir: path.resolve(import.meta.dirname, '..') },
      bundle: true,
      splitting: true,
      format: 'esm',
      entryNames: 'app',
      chunkNames: 'chunks/[name]-[hash]',
      outdir: path.join(root, 'dist'),
    });
    const spec = { nTime: 2, nBand: 1, height: 2, width: 2, chunk: 2, sharded: false, consolidated: true };
    await writeTree(path.join(root, 'store'), compressedStore(spec, COMPRESSORS.zstd));
    await writeFile(path.join(root, 'index.html'), '<!doctype html><title>Bundled reader</title>');
    server = await startStaticServer(root);

    const requests = [];
    const codecPath = /^\/dist\/chunks\/zstd-[A-Z0-9]+\.js$/;
    await page.route((url) => url.origin === server.url && codecPath.test(url.pathname), (route) => {
      const url = new URL(route.request().url());
      requests.push({ path: url.pathname, query: url.search });
      return requests.length === 1 ? route.fulfill({ status: 503, body: 'temporarily unavailable' }) : route.continue();
    });
    await page.goto(`${server.url}/index.html`);
    const result = await page.evaluate(async () => {
      const { openStore } = await import('/dist/app.js');
      const store = await openStore(new URL('/store/', location.href).href, { workers: 0 });
      const read = (t) => store.getRaw(0, 0, 0, t).then((data) => Array.from(data), (error) => error.message);
      try {
        return [await read(0), await read(0), await read(1)];
      } finally {
        store.close();
      }
    });

    const expected503 = consoleErrors.findIndex((line) => /Failed to load resource: .*503/.test(line));
    if (expected503 !== -1) consoleErrors.splice(expected503, 1);
    const firstPixels = [1000, 1013, 1007, 1020];
    const [initial, ...recovered] = result;
    // Main-thread warmup can finish the failed import before the first chunk read starts decoding.
    if (typeof initial === 'string') expect(initial).toContain('Failed to fetch dynamically imported module');
    else expect(initial).toEqual(firstPixels);
    expect(recovered).toEqual([firstPixels, [1040, 1050, 1047, 1057]]);
    expect(requests).toEqual([{ path: expect.stringMatching(codecPath), query: '' }, { path: requests[0]?.path, query: '?retry=1' }]);
  } finally {
    await server?.close();
    await rm(root, { recursive: true, force: true });
  }
});
