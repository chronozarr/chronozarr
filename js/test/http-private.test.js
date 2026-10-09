import { test } from 'node:test';
import assert from 'node:assert/strict';
import { HttpStore } from '../chronozarr/http.js';
import { RequestLimiter } from '../chronozarr/limiter.js';
import { BandwidthEstimator } from '../chronozarr/bandwidth.js';

// docs/private.md states these behaviours of the reader.

function store(base, fetchImpl) {
  return new HttpStore(base, {
    fetch: fetchImpl,
    limiter: new RequestLimiter(1),
    network: { requests: 0, bytes: 0 },
    bandwidth: new BandwidthEstimator(() => performance.now()),
    retryDelaysMs: [],
  });
}

test('the query string of the store URL is sent with every object request', async () => {
  const urls = [];
  const http = store('https://data.example/prefix/store?Policy=P&Signature=S', async (request) => {
    urls.push(request.url);
    return new Response(Uint8Array.of(1, 2, 3, 4), { status: request.headers.has('Range') ? 206 : 200 });
  });
  await http.get('/zarr.json');
  await http.getRange('/0/data/c/0/0/0/0', { offset: 0, length: 2 });
  assert.deepEqual(urls, [
    'https://data.example/prefix/store/zarr.json?Policy=P&Signature=S',
    'https://data.example/prefix/store/0/data/c/0/0/0/0?Policy=P&Signature=S',
  ]);
});

test('requests carry no credentials option, so the browser default same-origin applies', async () => {
  const modes = [];
  const http = store('https://data.example/store', async (request) => {
    modes.push(request.credentials);
    return new Response(Uint8Array.of(1));
  });
  await http.get('/zarr.json');
  assert.deepEqual(modes, ['same-origin']);
});

test('a fetch option can ask for credentials on every request', async () => {
  const modes = [];
  const inner = async (request) => {
    modes.push(request.credentials);
    return new Response(Uint8Array.of(1));
  };
  const http = store('https://data.example/store', (request) => inner(new Request(request, { credentials: 'include' })));
  await http.get('/zarr.json');
  assert.deepEqual(modes, ['include']);
});
