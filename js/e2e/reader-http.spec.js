import { test, expect } from '@playwright/test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import path from 'node:path';
import { startStaticServer } from '../support/static-server.js';
import { buildSyntheticStore, sourceValue } from '../support/synthetic-store.js';

// Exercise browser fetch itself: scripted Response objects cannot reproduce a
// socket closing halfway through a body or cancellation reaching the server.
test('reader recovers from HTTP and body failures, then cancels a hanging request', async ({ page }) => {
  const files = buildSyntheticStore({ nTime: 4, nBand: 1, height: 32, width: 32, chunk: 32, encoding: 'none', sharded: false }).files;
  let attempts = 0;
  let hangingStarted;
  let hangingClosed;
  const started = new Promise(resolve => { hangingStarted = resolve; });
  const closed = new Promise(resolve => { hangingClosed = resolve; });
  const data = createServer((req, res) => {
    res.setHeader('Access-Control-Allow-Origin', '*');
    const key = new URL(req.url, 'http://localhost').pathname;
    if (key === '/0/data/c/2/0/0/0') {
      res.on('close', hangingClosed);
      hangingStarted();
      return;
    }
    if (key === '/0/data/c/1/0/0/0') {
      attempts++;
      if (attempts === 1) { res.writeHead(503); res.end('temporarily unavailable'); return; }
      if (attempts === 2) {
        res.writeHead(200, { 'Content-Length': files.get(key).length });
        res.write(files.get(key).slice(0, 8));
        setImmediate(() => res.destroy());
        return;
      }
    }
    const bytes = files.get(key);
    res.writeHead(bytes ? 200 : 404);
    res.end(bytes);
  });
  await new Promise(resolve => data.listen(0, '127.0.0.1', resolve));
  const app = await startStaticServer(path.resolve(import.meta.dirname, '..'));
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(`${app.url}/support/reader-bench/bench.html`);
    const base = `http://127.0.0.1:${data.address().port}`;
    const value = await page.evaluate(async base => {
      const { openStore } = await import('/chronozarr/decoder.js');
      window.httpStore = await openStore(base, { workers: 0, retryDelaysMs: [0, 0] });
      const raw = await window.httpStore.getRaw(0, 0, 0, 1);
      return raw[5 * 32 + 7];
    }, base);
    expect(value).toBe(sourceValue(1, 0, 5, 7));
    expect(attempts).toBe(3);
    await page.evaluate(() => {
      window.abortRead = new AbortController();
      window.readResult = window.httpStore.getRaw(0, 0, 0, 2, { signal: window.abortRead.signal }).catch(error => error.name);
    });
    await started;
    await page.evaluate(() => window.abortRead.abort());
    expect(await page.evaluate(() => window.readResult)).toBe('AbortError');
    await closed;
    await expect.poll(() => page.evaluate(() => window.httpStore.stats.network.inflight)).toBe(0);
    expect(await page.evaluate(async () => (await window.httpStore.getRaw(0, 0, 0, 3)).length)).toBe(1024);
    await page.evaluate(() => window.httpStore.close());
    assert.deepEqual(errors, []);
  } finally {
    data.closeAllConnections();
    await new Promise(resolve => data.close(resolve));
    await app.close();
  }
});
