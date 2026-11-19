import path from 'node:path';
import { startStaticServer } from '../support/static-server.js';
import { expect, test } from './fixtures.js';

test('worker decodes leave custom store buffers intact for later reads', async ({ page }) => {
  const source = await startStaticServer(path.resolve(import.meta.dirname, '..'));
  try {
    await page.route(`${source.url}/reader-probe.html`, (route) =>
      route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Worker reader</title>' }),
    );
    await page.goto(`${source.url}/reader-probe.html`);
    const probe = await page.evaluateHandle(async () => {
      const { openStore } = await import('/chronozarr/decoder.js');
      const { buildSyntheticStore } = await import('/support/synthetic-store.js');
      const { files } = buildSyntheticStore({ nTime: 2, nBand: 1, height: 2, width: 2, chunk: 2, nodata: null });
      const chunks = [0, 1].map((t) => files.get(`/0/data/c/${t}/0/0/0`));
      const ready = Promise.withResolvers();
      let decoded = 0;
      const store = await openStore(new URL('/fixture/', location.href).href, {
        store: { async get(key) { return files.get(key); } },
        workers: 1,
        compressedBytes: 0,
        spawnWorker() {
          const worker = new Worker('/chronozarr/decode-worker.js', { type: 'module' });
          return {
            postMessage(...args) { worker.postMessage(...args); },
            terminate() { worker.terminate(); },
            set onmessage(handle) {
              worker.onmessage = handle && ((event) => {
                const { type, id, message } = event.data;
                if (type === 'decoded') decoded++;
                handle(event);
                // Wait until the pool has processed the real worker's ready reply.
                if (type === 'ready') ready.resolve();
                if (type === 'error' && id === undefined) ready.reject(new Error(message));
              });
            },
            set onerror(handle) {
              worker.onerror = handle && ((event) => {
                handle(event);
                ready.reject(new Error(event.message));
              });
            },
          };
        },
      });
      return {
        store,
        async read(t) {
          await ready.promise;
          const pixels = [...await store.getRaw(0, 0, 0, t)];
          const buffers = chunks.map((bytes) => bytes.byteLength === 0 ? [] : [...bytes]);
          return { pixels, buffers, decoded };
        },
        original: chunks.map((bytes) => [...bytes]),
      };
    });
    try {
      const original = await probe.evaluate(({ original }) => original);
      const first = [1000, 1013, 1007, 1020];
      const second = [1040, 1050, 1047, 1057];
      expect(await probe.evaluate(({ read }) => read(0))).toEqual({ pixels: first, buffers: original, decoded: 1 });
      expect(await probe.evaluate(({ store, read }) => {
        store.clearCache();
        return read(0);
      })).toEqual({ pixels: first, buffers: original, decoded: 2 });
      expect(await probe.evaluate(({ read }) => read(1))).toEqual({ pixels: second, buffers: original, decoded: 3 });
    } finally {
      await probe.evaluate(({ store }) => store.close());
      await probe.dispose();
    }
  } finally {
    await source.close();
  }
});
