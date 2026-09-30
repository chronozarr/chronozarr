import { test } from 'node:test';
import assert from 'node:assert/strict';
import { indexByteLength, parseShardIndex, shardIndexRange } from '../chronozarr/shard.js';

test('the index read: prefix for a start index; exact range when the shard size is known; suffix otherwise', () => {
  assert.equal(indexByteLength(4, true), 68);
  assert.deepEqual(shardIndexRange(4, true, true, 1000), { offset: 0, length: 68 }, 'start index: the first bytes, shard size irrelevant');
  assert.deepEqual(shardIndexRange(4, true, false, 1000), { offset: 932, length: 68 }, 'end index with a known size: bounded range, no HEAD');
  assert.deepEqual(shardIndexRange(4, true, false, undefined), { suffixLength: 68 });
  assert.deepEqual(shardIndexRange(4, false, false, 1000), { offset: 936, length: 64 }, 'no crc codec: 16 bytes per entry only');
});

test('a size smaller than the index itself is not trusted', () => {
  assert.deepEqual(shardIndexRange(4, true, false, 10), { suffixLength: 68 });
  assert.deepEqual(shardIndexRange(4, true, false, 68.5), { suffixLength: 68 });
  assert.deepEqual(shardIndexRange(4, true, false, 68), { offset: 0, length: 68 }, 'a shard that is all index');
});

test('the index of a partial last time shard has the full number of entries, the unused ones empty', () => {
  const view = new DataView(new ArrayBuffer(16 * 4));
  view.setBigUint64(0, 0n, true);
  view.setBigUint64(8, 50n, true);
  for (let i = 1; i < 4; i++) {
    view.setBigUint64(16 * i, 0xffffffffffffffffn, true);
    view.setBigUint64(16 * i + 8, 0xffffffffffffffffn, true);
  }
  const bytes = new Uint8Array(16 * 4);
  bytes.set(new Uint8Array(view.buffer));
  const entries = parseShardIndex(bytes, 4, false, 'partial');
  assert.deepEqual([...entries], [0, 50, -1, 0, -1, 0, -1, 0]);
});
