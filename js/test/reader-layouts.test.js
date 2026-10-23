import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openStore } from '../chronozarr/decoder.js';
import { buildSyntheticStore, crc32c } from '../support/synthetic-store.js';

const spec = { nTime: 5, nBand: 3, height: 7, width: 11, chunk: 4, sharded: false, mask: true, coverage: true };
const value = (t, b, y, x) => 100 + t * 8000 + b * 2000 + y * 30 + x;
const valid = (t, y, x) => (t + y + x) % 3 ? 1 : 0;
const observed = (t, y, x) => (t + y + x) % 8;
const json = (value) => new TextEncoder().encode(JSON.stringify(value));
const product = (shape) => shape.reduce((a, b) => a * b, 1);
function coordinates(shape) {
  return shape.reduce((points, count) => points.flatMap((point) => Array.from({ length: count }, (_, i) => [...point, i])), [[]]);
}

// The expected values are source functions, independent of the reader's chunk-selection logic.
function writeLayout(readable, kind, inner, { sharded = false, indexLocation = 'end', endian = 'little', keys = 'default', lod = 0 } = {}) {
  const prefix = `/${lod}/${kind}`;
  const meta = JSON.parse(new TextDecoder().decode(readable.files.get(`${prefix}/zarr.json`)));
  for (const key of [...readable.files.keys()]) if (key.startsWith(`${prefix}/c/`)) readable.files.delete(key);
  const rank = inner.length;
  const outer = inner.map((n) => n * 2);
  const bytesCodec = { name: 'bytes', configuration: { endian } };
  meta.chunk_grid.configuration.chunk_shape = sharded ? outer : inner;
  meta.chunk_key_encoding = { name: keys, configuration: { separator: keys === 'v2' ? '.' : '/' } };
  const keyOf = (coords) => (keys === 'v2' ? coords.join('.') : ['c', ...coords].join('/'));
  meta.codecs = sharded ? [{ name: 'sharding_indexed', configuration: { chunk_shape: inner, codecs: [bytesCodec], index_codecs: [{ name: 'bytes', configuration: { endian: 'little' } }, { name: 'crc32c' }], index_location: indexLocation } }] : [bytesCodec];
  readable.files.set(`${prefix}/zarr.json`, json(meta));
  const shards = new Map();
  for (const coords of coordinates(meta.shape.map((n, i) => Math.ceil(n / inner[i])))) {
    const bytes = new Uint8Array(product(inner) * (kind === 'data' ? 2 : 1));
    const view = new DataView(bytes.buffer);
    coordinates(inner).forEach((local, index) => {
      const p = coords.map((n, i) => n * inner[i] + local[i]);
      if (p.some((n, i) => n >= meta.shape[i])) return;
      const [t, b, y, x] = rank === 4 ? p : [p[0], 0, p[1], p[2]];
      if (kind === 'data') view.setUint16(index * 2, value(t, b, y, x), endian === 'little');
      else bytes[index] = kind === 'mask' ? valid(t, y, x) : observed(t, y, x);
    });
    if (!sharded) readable.files.set(`${prefix}/${keyOf(coords)}`, bytes);
    else {
      const shardCoords = coords.map((n) => Math.floor(n / 2));
      const key = `${prefix}/${keyOf(shardCoords)}`;
      const chunks = shards.get(key) ?? Array(2 ** rank).fill(null);
      chunks[coords.reduce((n, coordinate) => n * 2 + coordinate % 2, 0)] = bytes;
      shards.set(key, chunks);
    }
  }
  for (const [key, chunks] of shards) {
    const index = new Uint8Array(chunks.length * 16 + 4);
    const view = new DataView(index.buffer);
    let offset = indexLocation === 'start' ? index.length : 0;
    chunks.forEach((bytes, i) => {
      view.setBigUint64(i * 16, bytes ? BigInt(offset) : 0xffffffffffffffffn, true);
      view.setBigUint64(i * 16 + 8, bytes ? BigInt(bytes.length) : 0xffffffffffffffffn, true);
      if (bytes) offset += bytes.length;
    });
    view.setUint32(index.length - 4, crc32c(index.subarray(0, -4)), true);
    const pieces = indexLocation === 'start' ? [index, ...chunks.filter(Boolean)] : [...chunks.filter(Boolean), index];
    const shard = new Uint8Array(pieces.reduce((n, bytes) => n + bytes.length, 0));
    let at = 0;
    for (const bytes of pieces) { shard.set(bytes, at); at += bytes.length; }
    readable.files.set(key, shard);
  }
}

for (const options of [
  { label: 'unsharded time/band splits and nonsquare cells' },
  { label: 'v2 dot keys and big-endian bytes', keys: 'v2', endian: 'big' },
  { label: 'spatial/band/time sharding, index at end', sharded: true },
  { label: 'spatial/band/time sharding, index at start', sharded: true, indexLocation: 'start' },
]) {
  test(`reader returns exact values and independent planes for ${options.label}`, async () => {
    const readable = buildSyntheticStore(spec);
    writeLayout(readable, 'data', [2, 1, 3, 5], options);
    writeLayout(readable, 'mask', [3, 2, 4], options);
    writeLayout(readable, 'coverage', [2, 4, 3], options);
    const store = await openStore('memory://layouts', { store: readable, workers: 0 });
    assert.ok(store.notes.some((note) => /chunk layout/.test(note)));
    const level = store.levels[0];
    assert.deepEqual([level.chunkHeight, level.chunkWidth], [3, 5]);
    for (let t = 0; t < spec.nTime; t++) for (let row = 0; row < level.gridRows; row++) for (let col = 0; col < level.gridCols; col++) {
      const [data, mask, coverage] = await Promise.all([store.getRaw(0, row, col, t), store.getMask(0, row, col, t), store.getCoverage(0, row, col, t)]);
      assert.equal(data.length, spec.nBand * 15);
      assert.equal(mask.length, 15);
      for (let y = 0; y < 3; y++) for (let x = 0; x < 5; x++) {
        const gy = row * 3 + y, gx = col * 5 + x;
        const inside = gy < spec.height && gx < spec.width;
        for (let b = 0; b < spec.nBand; b++) assert.equal(data[b * 15 + y * 5 + x], inside ? value(t, b, gy, gx) : 0);
        assert.equal(mask[y * 5 + x], inside ? valid(t, gy, gx) : 0);
        assert.equal(coverage[y * 5 + x], inside ? observed(t, gy, gx) : 0);
      }
    }
    const requests = readable.log.length;
    await store.getRaw(0, 0, 0, 3);
    await store.getMask(0, 0, 0, 3);
    assert.equal(readable.log.length, requests, 'warm logical cells need no wire reads');
    store.close();
  });
}

test('a malformed general-layout chunk refuses instead of silently filling missing decoded elements', async () => {
  const readable = buildSyntheticStore(spec);
  writeLayout(readable, 'data', [2, 1, 3, 5]);
  readable.files.set('/0/data/c/0/0/0/0', Uint8Array.of(0, 0));
  const store = await openStore('memory://bad-layout', { store: readable, workers: 0 });
  await assert.rejects(store.getRaw(0, 0, 0, 0), /decoded chunk.*declared/);
  store.close();
});

test('a replaced mutable generic shard refreshes its index while retaining the opening time snapshot', async () => {
  const readable = buildSyntheticStore(spec);
  writeLayout(readable, 'data', [2, 1, 3, 5], { sharded: true });
  const store = await openStore('memory://growing-layout', { store: readable, workers: 0 });
  assert.equal((await store.getRaw(0, 0, 0, 4))[0], value(4, 0, 0, 0));
  store.clearCache();
  // Model append replacing the trailing shards with old encoded chunks at new offsets.
  for (const [key, bytes] of [...readable.files]) if (key.startsWith('/0/data/c/1/')) {
    const indexBytes = 16 * 16 + 4;
    const payload = bytes.subarray(0, -indexBytes);
    const index = bytes.slice(-indexBytes);
    const view = new DataView(index.buffer);
    for (let i = 0; i < 16; i++) if (view.getBigUint64(i * 16, true) !== 0xffffffffffffffffn) view.setBigUint64(i * 16, view.getBigUint64(i * 16, true) + 2n, true);
    view.setUint32(index.length - 4, crc32c(index.subarray(0, -4)), true);
    const replaced = new Uint8Array(bytes.length + 2);
    replaced.set(payload, 2);
    replaced.set(index, payload.length + 2);
    readable.files.set(key, replaced);
  }
  const root = JSON.parse(new TextDecoder().decode(readable.files.get('/zarr.json')));
  root.attributes.chronozarr.times.push('2024-01-06T00:00:00Z');
  readable.files.set('/zarr.json', json(root));
  assert.equal((await store.getRaw(0, 0, 0, 4))[0], value(4, 0, 0, 0));
  assert.equal(store.times.length, 5, 'an open reader remains the selected metadata snapshot');
  await assert.rejects(store.getRaw(0, 0, 0, 5), /out of range/);
  store.close();
});

test('absent general-layout data chunks use their declared nonzero fill and masks use their own fill', async () => {
  const readable = buildSyntheticStore(spec);
  writeLayout(readable, 'data', [2, 1, 3, 5]);
  writeLayout(readable, 'mask', [3, 2, 4]);
  const metadata = JSON.parse(new TextDecoder().decode(readable.files.get('/0/data/zarr.json')));
  metadata.fill_value = 42;
  readable.files.set('/0/data/zarr.json', json(metadata));
  const root = JSON.parse(new TextDecoder().decode(readable.files.get('/zarr.json')));
  root.attributes.chronozarr.nodata = 42;
  readable.files.set('/zarr.json', json(root));
  readable.files.delete('/0/data/c/0/0/0/0');
  for (const key of [...readable.files.keys()]) if (key.startsWith('/0/mask/c/0/')) readable.files.delete(key);
  const store = await openStore('memory://missing-layout', { store: readable, workers: 0 });
  const data = await store.getRaw(0, 0, 0, 1);
  assert.ok(data.subarray(0, 15).every((n) => n === 42));
  assert.equal(data[15], value(1, 1, 0, 0));
  assert.ok((await store.getMask(0, 0, 0, 1)).every((n) => n === 0));
  store.close();
});

for (const [label, change, reason] of [
  ['big-endian index', (m) => { m.codecs[0].configuration.index_codecs[0].configuration.endian = 'big'; }, /big-endian shard index/],
  ['unknown index location', (m) => { m.codecs[0].configuration.index_location = 'middle'; }, /index_location middle/],
  ['outer shard codec', (m) => { m.codecs.push({ name: 'gzip' }); }, /codecs outside sharding_indexed/],
]) test(`unsupported ${label} fails before returning measurements`, async () => {
  const readable = buildSyntheticStore(spec);
  writeLayout(readable, 'data', [2, 1, 3, 5], { sharded: true });
  const metadata = JSON.parse(new TextDecoder().decode(readable.files.get('/0/data/zarr.json')));
  change(metadata);
  readable.files.set('/0/data/zarr.json', json(metadata));
  await assert.rejects(openStore('memory://unsupported-shard', { store: readable, workers: 0 }), reason);
  assert.ok(readable.log.every(({ key }) => key.endsWith('zarr.json')));
});

test('different stored chunk shapes across levels share one logical cell shape for GPU consumers', async () => {
  const readable = buildSyntheticStore({ ...spec, nLevels: 2 });
  writeLayout(readable, 'data', [2, 1, 3, 5]);
  writeLayout(readable, 'data', [1, 3, 2, 4], { lod: 1 });
  const store = await openStore('memory://different-level-chunks', { store: readable, workers: 0 });
  assert.deepEqual(store.levels.map(({ chunkHeight, chunkWidth }) => [chunkHeight, chunkWidth]), [[3, 5], [3, 5]]);
  assert.ok(store.notes.some((note) => /level 1 stored chunks.*reader cells/.test(note)));
  const level = store.levels[1];
  for (let row = 0; row < level.gridRows; row++) for (let col = 0; col < level.gridCols; col++) {
    const data = await store.getRaw(1, row, col, 3);
    for (let b = 0; b < 3; b++) for (let y = 0; y < 3; y++) for (let x = 0; x < 5; x++) {
      const gy = row * 3 + y, gx = col * 5 + x;
      assert.equal(data[b * 15 + y * 5 + x], gy < level.height && gx < level.width ? value(3, b, gy, gx) : 0);
    }
  }
  store.close();
});
