import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openStore } from '../chronozarr/decoder.js';
import { HttpStore } from '../chronozarr/http.js';
import { RequestLimiter } from '../chronozarr/limiter.js';
import { BandwidthEstimator } from '../chronozarr/bandwidth.js';
import { filesFetch } from '../support/files-fetch.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

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
    if (request.headers.has('Range')) {
      return new Response(Uint8Array.of(1, 2), { status: 206, headers: { 'Content-Range': 'bytes 0-1/4' } });
    }
    return new Response(Uint8Array.of(1, 2, 3, 4));
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

test('retry and terminal diagnostics redact a signed IPv6 request echoed by fetch', async (t) => {
  const warn = t.mock.method(console, 'warn', () => {});
  const error = t.mock.method(console, 'error', () => {});
  await assert.rejects(openStore('https://[::1]:8443/data?token=SYNTHETIC_TOKEN', {
    workers: 0,
    retryDelaysMs: [0],
    fetch: async (request) => { throw new Error(`Network request failed for ${request.url}`); },
  }), {
    message: 'GET https://[::1]:8443/data/zarr.json: Error: Network request failed for https://[::1]:8443/data/zarr.json (2 attempts)',
  });
  assert.equal(warn.mock.callCount(), 1);
  assert.equal(error.mock.callCount(), 1);
  for (const log of [warn, error]) {
    const [, details] = log.mock.calls[0].arguments;
    assert.equal(details.url, 'https://[::1]:8443/data/zarr.json');
    assert.equal(details.error, 'Error: Network request failed for https://[::1]:8443/data/zarr.json');
  }
});

test('a reflected HTTP reason is omitted from diagnostics', async (t) => {
  const log = t.mock.method(console, 'error', () => {});
  const http = store('https://data.example/prefix/store?token=SYNTHETIC_TOKEN', async (request) => {
    const { pathname, search } = new URL(request.url);
    return new Response(null, { status: 403, statusText: `Expired ${pathname}${search}` });
  });
  await assert.rejects(http.get('/zarr.json'), {
    message: 'GET https://data.example/prefix/store/zarr.json: HTTP 403 (1 attempt)',
  });
  assert.doesNotMatch(JSON.stringify(log.mock.calls[0].arguments), /SYNTHETIC_TOKEN/);
});

test('invalid store metadata does not echo the base query', async () => {
  await assert.rejects(openStore('https://[::1]/data?token=SYNTHETIC_TOKEN', {
    workers: 0,
    fetch: async () => new Response('{}'),
  }), {
    message: 'https://[::1]/data: not a valid chronozarr store: root must be a Zarr v3 group',
  });
});

test('an invalid JSON response does not repeat the response text', async () => {
  await assert.rejects(openStore('https://data.example/store?token=SYNTHETIC_TOKEN', {
    workers: 0,
    fetch: async () => new Response('SYNTHETIC_TOKEN'),
  }), {
    name: 'SyntaxError',
    message: 'https://data.example/store: root zarr.json does not contain valid JSON',
  });
});

test('user information in an HTTP store URL is refused without exposing credentials', async () => {
  await assert.rejects(openStore('https://alice:password@[::1]/data?token=SYNTHETIC_TOKEN'), {
    message: 'Store URL includes credentials: https://[::1]/data',
  });
});

for (const consolidated of [false, true]) {
  test(`custom array names keep literal URL punctuation in the object key (consolidated=${consolidated})`, async () => {
    const spec = { nTime: 2, nBand: 1, height: 16, width: 16, chunk: 16, consolidated, values: (t) => 1000 + t };
    const { files } = buildSyntheticStore(spec);
    const base = 'https://data.example/store?token=SYNTHETIC_TOKEN';
    const variable = 'depth#raw?test%2F';
    const encoded = 'depth%23raw%3Ftest%252F';
    const document = JSON.parse(new TextDecoder().decode(files.get('/zarr.json')));
    document.attributes.chronozarr.variable = variable;
    if (consolidated) {
      const meta = document.consolidated_metadata.metadata;
      meta[`0/${variable}`] = meta['0/data'];
      delete meta['0/data'];
    }
    files.set('/zarr.json', new TextEncoder().encode(JSON.stringify(document)));
    for (const [key, bytes] of files) {
      if (!key.startsWith('/0/data/')) continue;
      files.delete(key);
      files.set(key.replace('/0/data/', `/0/${encoded}/`), bytes);
    }
    const requests = [];
    const respond = filesFetch(files);
    const reader = await openStore(base, { workers: 0, fetch: (request) => {
      requests.push(request.url);
      return respond(request);
    } });
    assert.equal((await reader.getRaw(0, 0, 0, 1))[0], 1001);
    assert.equal(requests.at(-1), `https://data.example/store/0/${encoded}/c/1/0/0/0?token=SYNTHETIC_TOKEN`);
    assert.equal(requests.includes(`https://data.example/store/0/${encoded}/zarr.json?token=SYNTHETIC_TOKEN`), !consolidated);
    reader.close();
  });
}
