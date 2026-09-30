import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { openStore, applyDelta, chunkKey } from '../chronozarr/decoder.js';
import { buildSyntheticStore, sourceValue, crc32c } from '../support/synthetic-store.js';
import { startStaticServer } from '../support/static-server.js';

const REPO_ROOT = path.resolve(import.meta.dirname, '../..');
const FIXTURES = ['synthetic_sharded', 'synthetic_unsharded', 'synthetic_gzip'];
const fixtureDir = (name) => path.join(REPO_ROOT, 'data/spike', name);
const haveFixtures = FIXTURES.every((name) => existsSync(fixtureDir(name)));
const skipFixtures = haveFixtures ? false : 'data/spike fixtures not present';

async function bandSums(store, lod, t) {
  const level = store.levels[lod];
  const sums = new Array(level.nBand).fill(0);
  for (let row = 0; row < level.gridRows; row++) {
    for (let col = 0; col < level.gridCols; col++) {
      const cell = await store.getCell(lod, row, col, t);
      for (let b = 0; b < cell.bands; b++) {
        for (let y = 0; y < cell.height; y++) {
          for (let x = 0; x < cell.width; x++) sums[b] += cell.data[(b * cell.chunkHeight + y) * cell.chunkWidth + x];
        }
      }
    }
  }
  return sums;
}

test('chunkKey and applyDelta', () => {
  assert.equal(chunkKey(1, 2, 3, 4), '1/2/3/4');
  const anchor = Uint16Array.of(100, 100, 65535, 0);
  const residual = Int16Array.of(-150, 25, 10, -1);
  const out = applyDelta(anchor, new Uint16Array(residual.buffer));
  assert.deepEqual([...out], [0, 125, 65535, 0], 'clamps to 0..65535');
});

test('crc32c matches the standard check value', () => {
  assert.equal(crc32c(new TextEncoder().encode('123456789')), 0xe3069283);
});

for (const name of FIXTURES) {
  test(`fixture ${name}: every level and timestep matches expected.json`, { skip: skipFixtures }, async (t) => {
    const server = await startStaticServer(REPO_ROOT);
    t.after(() => server.close());
    const expected = JSON.parse(readFileSync(path.join(fixtureDir(name), 'expected.json'), 'utf8'));
    const store = await openStore(`${server.url}/data/spike/${name}/`);
    assert.deepEqual(store.bands, ['B04', 'B08']);
    assert.equal(store.times.length, 3);
    assert.equal(store.levels.length, 2);

    for (const [lodKey, exp] of Object.entries(expected)) {
      const lod = Number(lodKey);
      for (let tt = 0; tt < exp.sum_per_t_b.length; tt++) {
        assert.deepEqual(await bandSums(store, lod, tt), exp.sum_per_t_b[tt], `${name} lod ${lod} t ${tt} band sums`);
      }
      const { t: st, b, y, x, value } = exp.sample;
      const row = Math.floor(y / 512);
      const col = Math.floor(x / 512);
      const cell = await store.getCell(lod, row, col, st);
      const ly = y - row * 512;
      const lx = x - col * 512;
      assert.equal(cell.data[(b * 512 + ly) * 512 + lx], value, `${name} lod ${lod} sample pixel`);
      assert.equal(store.samplePixel(lod, row, col, st, lx, ly)[b], value, `${name} lod ${lod} samplePixel`);
    }
  });
}

test('fixture sharded: one shard index read per cell, then one range request per timestep', { skip: skipFixtures }, async (t) => {
  const server = await startStaticServer(REPO_ROOT);
  t.after(() => server.close());
  const base = `${server.url}/data/spike/synthetic_sharded/`;
  const shardPath = '/data/spike/synthetic_sharded/0/data/c/0/0/0/0';
  const shardRequests = () => server.requests.filter((r) => r.path === shardPath);

  const store = await openStore(base);
  assert.equal(server.requests.length, 1, 'cold open is one request (consolidated metadata in root zarr.json)');
  assert.equal(store.stats.network.requests, 1);

  for (const tt of [0, 1, 2]) await store.getRaw(0, 0, 0, tt);
  const first = shardRequests();
  assert.deepEqual(
    first.map((r) => r.method),
    ['HEAD', 'GET', 'GET', 'GET', 'GET'],
    'index = HEAD + suffix range once, then exactly 3 chunk ranges',
  );
  for (const r of first.slice(2)) assert.match(r.range, /^bytes=\d+-\d+$/);

  for (const tt of [2, 1, 0]) await store.getRaw(0, 0, 0, tt);
  assert.equal(shardRequests().length, 5, 'cached chunks are never refetched');

  const stats = store.stats;
  assert.equal(stats.cache.hits, 3);
  assert.equal(stats.cache.misses, 3);
  assert.equal(stats.network.requests, 1 + 5);
});

test('fixture sharded: suffixRequests reads the index in one request', { skip: skipFixtures }, async (t) => {
  const server = await startStaticServer(REPO_ROOT);
  t.after(() => server.close());
  const store = await openStore(`${server.url}/data/spike/synthetic_sharded/`, { suffixRequests: true });
  await store.getRaw(0, 0, 0, 1);
  const shard = server.requests.filter((r) => r.path.endsWith('0/data/c/0/0/0/0'));
  assert.deepEqual(
    shard.map((r) => [r.method, r.range?.startsWith('bytes=-') ?? false]),
    [['GET', true], ['GET', false]],
  );
});

test('fixture unsharded: one plain GET per chunk, no HEAD, no range', { skip: skipFixtures }, async (t) => {
  const server = await startStaticServer(REPO_ROOT);
  t.after(() => server.close());
  const store = await openStore(`${server.url}/data/spike/synthetic_unsharded/`);
  for (const tt of [0, 1, 2]) await store.getRaw(0, 0, 0, tt);
  const chunkRequests = server.requests.filter((r) => r.path.includes('/0/data/c/'));
  assert.equal(chunkRequests.length, 3);
  assert.ok(chunkRequests.every((r) => r.method === 'GET' && r.range === null));
});

for (const indexLocation of ['end', 'start']) {
  test(`synthetic sharded store (index at ${indexLocation}): lossless star-delta roundtrip`, async () => {
    const spec = { nTime: 7, nBand: 3, height: 70, width: 45, chunk: 32, anchorInterval: 3, sharded: true, indexLocation };
    const store = await openStore('memory://synthetic', { store: buildSyntheticStore(spec) });
    assert.deepEqual(store.anchorIndices, [0, 3, 6]);
    const level = store.levels[0];
    assert.equal(level.gridRows, 3);
    assert.equal(level.gridCols, 2);
    for (let tt = 0; tt < spec.nTime; tt++) {
      for (let row = 0; row < level.gridRows; row++) {
        for (let col = 0; col < level.gridCols; col++) {
          const cell = await store.getCell(0, row, col, tt);
          const { width, height } = store.cellExtent(0, row, col);
          assert.equal(cell.width, width);
          assert.equal(cell.height, height);
          for (let b = 0; b < spec.nBand; b++) {
            for (let y = 0; y < height; y += 5) {
              for (let x = 0; x < width; x += 3) {
                const want = sourceValue(tt, b, row * 32 + y, col * 32 + x);
                assert.equal(cell.data[(b * 32 + y) * 32 + x], want, `t${tt} r${row} c${col} b${b} y${y} x${x}`);
                assert.equal(store.samplePixel(0, row, col, tt, x, y)[b], want);
              }
            }
          }
        }
      }
    }
  });
}

test('start-indexed shard: index read is a prefix range, not a suffix range', async () => {
  const readable = buildSyntheticStore({ nTime: 4, nBand: 1, height: 20, width: 20, chunk: 32, anchorInterval: 2, sharded: true, indexLocation: 'start' });
  const store = await openStore('memory://synthetic', { store: readable });
  await store.getRaw(0, 0, 0, 1);
  const shardCalls = readable.log.filter((c) => c.key === '/0/data/c/0/0/0/0');
  assert.deepEqual(shardCalls[0].range, { offset: 0, length: 16 * 4 + 4 });
  assert.equal(shardCalls.length, 2, 'index prefix read, then the one requested chunk');
});

test('unsharded synthetic store decodes', async () => {
  const spec = { nTime: 4, nBand: 2, height: 40, width: 33, chunk: 32, anchorInterval: 2, sharded: false };
  const store = await openStore('memory://synthetic', { store: buildSyntheticStore(spec) });
  const cell = await store.getCell(0, 1, 1, 3);
  assert.equal(cell.data[(1 * 32 + 2) * 32 + 0], sourceValue(3, 1, 34, 32));
});

test('getCell returns the anchor array itself for anchors and a new array for deltas', async () => {
  const spec = { nTime: 4, nBand: 1, height: 16, width: 16, chunk: 32, anchorInterval: 2, sharded: true };
  const store = await openStore('memory://synthetic', { store: buildSyntheticStore(spec) });
  const anchorCell = await store.getCell(0, 0, 0, 2);
  assert.equal(anchorCell.data, store.peekRaw(0, 0, 0, 2));
  const deltaCell = await store.getCell(0, 0, 0, 3);
  assert.notEqual(deltaCell.data, store.peekRaw(0, 0, 0, 3));
});

test('samplePixel returns null until both anchor and delta are cached', async () => {
  const spec = { nTime: 4, nBand: 1, height: 16, width: 16, chunk: 32, anchorInterval: 2, sharded: true };
  const store = await openStore('memory://synthetic', { store: buildSyntheticStore(spec) });
  assert.equal(store.samplePixel(0, 0, 0, 3, 1, 1), null);
  await store.getRaw(0, 0, 0, 2);
  assert.equal(store.samplePixel(0, 0, 0, 3, 1, 1), null, 'anchor alone is not enough for a delta timestep');
  await store.getRaw(0, 0, 0, 3);
  assert.equal(store.samplePixel(0, 0, 0, 3, 1, 1)[0], sourceValue(3, 0, 1, 1));
});

test('concurrent requests for one chunk share one fetch', async () => {
  const readable = buildSyntheticStore({ nTime: 4, nBand: 1, height: 16, width: 16, chunk: 32, anchorInterval: 2, sharded: false });
  const store = await openStore('memory://synthetic', { store: readable });
  const before = readable.log.length;
  const [a, b] = await Promise.all([store.getRaw(0, 0, 0, 1), store.getRaw(0, 0, 0, 1)]);
  assert.equal(a, b);
  assert.equal(readable.log.length - before, 1);
});

test('prefetch: anchors first, then deltas outward from t in the scrub direction', async () => {
  const spec = { nTime: 12, nBand: 1, height: 16, width: 16, chunk: 32, anchorInterval: 4, sharded: true };
  const run = async (direction) => {
    const store = await openStore('memory://synthetic', { store: buildSyntheticStore(spec) });
    const order = [];
    const result = await store.prefetch({
      lod: 0,
      cells: [[0, 0]],
      t: 5,
      direction,
      concurrency: 1,
      onChunk: (lod, row, col, t) => order.push(t),
    });
    assert.equal(result.errors.length, 0);
    return order;
  };
  const forward = await run(1);
  assert.deepEqual(forward.slice(0, 3), [4, 8, 0], 'anchors nearest to t=5 first');
  assert.deepEqual(forward.slice(3), [6, 7, 3, 2, 9, 1, 10, 11], 'forward neighbour first');
  const backward = await run(-1);
  assert.deepEqual(backward.slice(0, 3), [4, 8, 0]);
  assert.deepEqual(backward.slice(3), [6, 3, 7, 2, 1, 9, 10, 11], 'backward neighbour first');
});

test('prefetch: skips cached chunks, honours abort and the cache budget without evicting', async () => {
  const spec = { nTime: 10, nBand: 1, height: 16, width: 16, chunk: 32, anchorInterval: 3, sharded: true };
  const chunkBytes = 32 * 32 * 2;
  const store = await openStore('memory://synthetic', { store: buildSyntheticStore(spec), maxCacheBytes: chunkBytes * 5 });
  await store.getRaw(0, 0, 0, 0);
  const result = await store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 1 });
  assert.equal(result.skipped, 1, 'the cached anchor 0 is skipped');
  assert.equal(result.budgetReached, true);
  assert.equal(store.cacheInfo().entries, 5);
  assert.equal(store.stats.cache.evictions, 0);

  const aborted = new AbortController();
  aborted.abort();
  const fresh = await openStore('memory://synthetic', { store: buildSyntheticStore(spec) });
  const none = await fresh.prefetch({ lod: 0, cells: [[0, 0]], t: 0, signal: aborted.signal });
  assert.equal(none.fetched, 0);
});

test('demand loads evict deltas before anchors', async () => {
  const spec = { nTime: 10, nBand: 1, height: 16, width: 16, chunk: 32, anchorInterval: 3, sharded: true };
  const chunkBytes = 32 * 32 * 2;
  const store = await openStore('memory://synthetic', { store: buildSyntheticStore(spec), maxCacheBytes: chunkBytes * 3 });
  await store.getRaw(0, 0, 0, 0);
  await store.getRaw(0, 0, 0, 1);
  await store.getRaw(0, 0, 0, 2);
  await store.getRaw(0, 0, 0, 4);
  assert.equal(store.peekRaw(0, 0, 0, 0) instanceof Uint16Array, true, 'anchor survives');
  assert.equal(store.peekRaw(0, 0, 0, 1), undefined, 'oldest delta evicted');
  assert.equal(store.stats.cache.evictions, 1);
  assert.ok(store.cacheInfo().bytes <= chunkBytes * 3);
});

test('errors carry the store URL and the reason', async () => {
  await assert.rejects(openStore('memory://empty', { store: { get: async () => undefined, getRange: async () => undefined } }), /memory:\/\/empty.*root zarr\.json not found/);
  const readable = buildSyntheticStore({ nTime: 2, nBand: 1, height: 16, width: 16, chunk: 32, anchorInterval: 2, sharded: true });
  const store = await openStore('memory://synthetic', { store: readable });
  await assert.rejects(store.getRaw(0, 5, 0, 0), /cell \(5, 0\) outside 1x1 grid at lod 0/);
  await assert.rejects(store.getRaw(0, 0, 0, 9), /timestep 9 out of range 0\.\.1/);
  await assert.rejects(store.getRaw(3, 0, 0, 0), /lod 3 out of range/);
});
