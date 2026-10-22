import { test } from 'node:test';
import assert from 'node:assert/strict';
import { prepareMovieCache } from '../demo/cache-policy.js';
import { openStore } from '../chronozarr/decoder.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

const shape = { nTime: 4, nBand: 1, height: 128, width: 128, chunk: 128, sharded: false };

for (const dtype of ['uint8', 'uint16', 'int16', 'float32']) {
  for (const oversized of [false, true]) {
    test(`viewer cache policy: ${oversized ? 'oversized' : 'fitting'} ${dtype} timeline`, async () => {
      const bytes = { uint8: 1, uint16: 2, int16: 2, float32: 4 }[dtype];
      const totalBytes = 128 * 128 * bytes * (oversized ? 2 : 6);
      const store = await openStore('memory://movie-policy', { store: buildSyntheticStore({ ...shape, dtype }), workers: 0, totalBytes });
      try {
        const expected = (await store.getRaw(0, 0, 0, 0)).slice();
        const enabled = prepareMovieCache(store);
        assert.equal(enabled, oversized && dtype === 'uint16');
        assert.equal(store.budgets().totalBytes, totalBytes);
        if (enabled) {
          assert.equal(store.hasCachedRaw(0, 0, 0, 0), false, 'switching representation discards the old raw cache once');
          await store.getRaw(0, 0, 0, 0);
          const packed = store.peekCompact(0, 0, 0, 0);
          assert.ok(packed, 'a packed chunk is available for direct GPU upload');
          packed.unpack = () => { throw new Error('readiness must not unpack'); };
          assert.equal(store.hasCachedRaw(0, 0, 0, 0), true);
          assert.equal(store.peekCompact(0, 0, 0, 0), packed);
          assert.equal(prepareMovieCache(store), true);
          assert.equal(store.hasCachedRaw(0, 0, 0, 0), true, 'repeated policy evaluation keeps existing packed data');
        } else {
          assert.deepEqual(store.peekRaw(0, 0, 0, 0), expected, 'ordinary stores keep their existing cache');
          assert.equal(store.peekCompact(0, 0, 0, 0), null);
        }
        assert.ok(store.stats.cache.usedBytes <= totalBytes);
      } finally { store.close(); }
    });
  }
}

test('a mask-only loop limit does not enable compact imagery when the imagery fits', async () => {
  const store = await openStore('memory://mask-policy', { store: buildSyntheticStore({ ...shape, dtype: 'uint16', mask: true }), workers: 0, totalBytes: 1024 * 1024 });
  try {
    assert.equal(store.hasMask, true);
    // Isolate a mask-limited movie independently of the default auxiliary minimum.
    store.loopFits = () => false;
    assert.equal(prepareMovieCache(store), false);
    assert.equal(store.compactCacheEnabled, false);
  } finally { store.close(); }
});
