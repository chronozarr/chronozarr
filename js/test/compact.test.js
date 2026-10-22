import { test } from 'node:test';
import assert from 'node:assert/strict';
import { packUint16, unpackUint16, compactUint16At } from '../chronozarr/compact.js';
import { ChunkCache } from '../chronozarr/cache.js';

test('compact uint16 blocks round-trip every useful residual width and partial block', () => {
  for (let bits = 0; bits <= 15; bits++) {
    const data = new Uint16Array(128 * 3 + 17);
    const range = (1 << bits) - 1;
    for (let i = 0; i < data.length; i++) data[i] = 179 + ((i * 5381) & range);
    data[0] = 179; data[1] = 179 + range;
    const packed = packUint16(data);
    if (!packed) { assert.ok(bits >= 15); continue; }
    assert.deepEqual(unpackUint16(packed), data);
    for (let i = 0; i < data.length; i++) assert.equal(compactUint16At(packed, i), data[i]);
    assert.equal(packed.byteLength, packed.words.byteLength + packed.blocks.byteLength);
    assert.ok(packed.byteLength < data.byteLength);
  }
});

test('compact uint16 handles zeros, full-range fallback, and dtype guard', () => {
  const uniform = new Uint16Array(512).fill(65535);
  assert.deepEqual(packUint16(uniform).unpack(), uniform);
  const data = new Uint16Array(512);
  for (let i = 0; i < data.length; i++) data[i] = i % 2 ? 65535 : 0;
  assert.equal(packUint16(data), null);
  assert.equal(packUint16(new Int16Array(512)), null);
});

test('cache accounts packed resident bytes and restores exact values without retaining a hidden array', () => {
  const raw = new Uint16Array(512).map((_, i) => 3000 + i % 127);
  const packed = packUint16(raw);
  const cache = new ChunkCache({ totalBytes: packed.byteLength * 2, score: e => -e.used });
  cache.insert('a', { t: 0 }, { data: packed });
  assert.equal(cache.usedBytes, packed.byteLength);
  assert.equal(cache.hasDecoded('a'), true);
  assert.deepEqual(cache.decoded('a'), raw);
  assert.equal(cache.get('a').data, packed);
  cache.insert('b', { t: 1 }, { data: packed });
  cache.insert('c', { t: 2 }, { data: packed });
  assert.equal(cache.hasDecoded('a'), false);
  assert.ok(cache.usedBytes <= cache.maxTotal);
});

test('reader compact mode exposes GPU backing and exact CPU reads within the unchanged cap', async () => {
  const { openStore } = await import('../chronozarr/decoder.js');
  const { buildSyntheticStore } = await import('../support/synthetic-store.js');
  const readable = buildSyntheticStore({ nTime: 3, nBand: 1, height: 128, width: 128, chunk: 128, sharded: false });
  const cap = 3 * 128 * 128 * 2;
  const store = await openStore('memory://compact', { store: readable, workers: 0, totalBytes: cap });
  try {
    const expected = (await store.getRaw(0, 0, 0, 0)).slice();
    assert.equal(store.enableCompactCache(), true);
    assert.equal(store.enableCompactCache(), true);
    await store.getRaw(0, 0, 0, 0);
    assert.equal(store.hasCachedRaw(0, 0, 0, 0), true);
    const packed = store.peekCompact(0, 0, 0, 0);
    assert.ok(packed);
    assert.deepEqual(packed.unpack(), expected);
    assert.deepEqual(store.peekRaw(0, 0, 0, 0), expected);
    assert.equal(store.samplePixel(0, 0, 0, 0, 17, 9)[0], expected[9 * 128 + 17]);
    const before = store.stats.network.requests;
    assert.deepEqual(await store.getRaw(0, 0, 0, 0), expected);
    assert.equal(store.stats.network.requests, before);
    assert.equal(store.budgets().totalBytes, cap);
    assert.ok(store.stats.cache.usedBytes <= cap);
    assert.equal(store.stats.cache.compressedBytes, 0);
    assert.equal(store.stats.cache.decodedBytes, packed.byteLength);
  } finally { store.close(); }
});

test('compact prefetch stops when decoded placement fails instead of using its disabled compressed tier', async () => {
  const { openStore } = await import('../chronozarr/decoder.js');
  const { buildSyntheticStore } = await import('../support/synthetic-store.js');
  const readable = buildSyntheticStore({ nTime: 4, nBand: 1, height: 128, width: 128, chunk: 128, sharded: false, values: (t, b, y, x) => x % 2 ? 65535 : 0 });
  const store = await openStore('memory://compact-full', { store: readable, workers: 0, totalBytes: 2 * 128 * 128 * 2, speculativeBytesInitial: 1e9 });
  try {
    store.enableCompactCache();
    store.evictionScore = entry => entry.t;
    await store.getRaw(0, 0, 0, 0);
    await store.getRaw(0, 0, 0, 1);
    const requests = store.stats.network.requests;
    const result = await store.prefetch({ lod: 0, cells: [[0, 0]], t: 2, loop: true });
    assert.equal(result.budgetReached, true);
    assert.equal(result.fetched, 0);
    assert.equal(store.stats.network.requests, requests, 'no fetch for a chunk that neither active tier can retain');
    assert.equal(store.stats.cache.compressedBytes, 0);
  } finally { store.close(); }
});


test('ensureRaw fills packed cache without expanding it and getRaw remains exact', async () => {
  const { openStore } = await import('../chronozarr/decoder.js');
  const { buildSyntheticStore } = await import('../support/synthetic-store.js');
  const store = await openStore('memory://ensure-packed', { store: buildSyntheticStore({ nTime: 3, nBand: 1, height: 128, width: 128, chunk: 128, sharded: false }), workers: 0 });
  try {
    const expected = (await store.getRaw(0, 0, 0, 1)).slice();
    store.enableCompactCache();
    assert.equal(await store.ensureRaw(0, 0, 0, 1), undefined);
    const packed = store.peekCompact(0, 0, 0, 1);
    assert.ok(packed);
    const unpack = packed.unpack;
    packed.unpack = () => { throw new Error('ensureRaw must not expand a chunk'); };
    const requests = store.stats.network.requests;
    assert.equal(await store.ensureRaw(0, 0, 0, 1), undefined);
    assert.equal(store.stats.network.requests, requests);
    packed.unpack = unpack;
    assert.deepEqual(await store.getRaw(0, 0, 0, 1), expected);
    assert.deepEqual(await store.getCell(0, 0, 0, 1).then(cell => cell.data), expected);
    await assert.rejects(store.ensureRaw(0, 0, 0, 3), RangeError);
    const controller = new AbortController(); controller.abort();
    await assert.rejects(store.ensureRaw(0, 0, 0, 2, { signal: controller.signal }), { name: 'AbortError' });
  } finally { store.close(); }
});
