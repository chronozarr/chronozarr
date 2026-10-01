import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ShardIndexError, crc32c, indexByteLength, parseShardIndex, shardIndexRange } from '../chronozarr/shard.js';

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

test('index errors are ShardIndexError: wrong length, wrong checksum, and chunks beyond the shard they were read from', () => {
  const entries = (pairs) => {
    const view = new DataView(new ArrayBuffer(16 * pairs.length));
    pairs.forEach(([offset, length], i) => {
      view.setBigUint64(16 * i, BigInt(offset), true);
      view.setBigUint64(16 * i + 8, BigInt(length), true);
    });
    return new Uint8Array(view.buffer);
  };
  const withCrc = (raw) => {
    const out = new Uint8Array(raw.length + 4);
    out.set(raw);
    new DataView(out.buffer).setUint32(raw.length, crc32c(raw), true);
    return out;
  };
  const raw = entries([[0, 50], [60, 30]]);
  assert.deepEqual([...parseShardIndex(withCrc(raw), 2, true, 'ok', 130)], [0, 50, 60, 30]);
  assert.throws(() => parseShardIndex(withCrc(raw).subarray(1), 2, true, 'short'), ShardIndexError);
  const flipped = withCrc(raw);
  flipped[3] ^= 0xff;
  assert.throws(() => parseShardIndex(flipped, 2, true, 'crc'), (error) => error instanceof ShardIndexError && /checksum mismatch/.test(error.message));

  // Without a checksum (index codecs of just `bytes`) the bounds are the only guard: bytes from inside a longer shard's data do not pass for an index.
  assert.deepEqual([...parseShardIndex(raw, 2, false, 'edge', 90)], [0, 50, 60, 30], 'a chunk ending exactly at the shard length is fine');
  assert.throws(() => parseShardIndex(raw, 2, false, 'beyond', 89), (error) => error instanceof ShardIndexError && /entry 1 ends at byte 90, beyond the 89-byte shard/.test(error.message));
  const garbage = new Uint8Array(32).fill(0xa5);
  assert.throws(() => parseShardIndex(garbage, 2, false, 'garbage', 5000), ShardIndexError);
  assert.deepEqual([...parseShardIndex(garbage.fill(0xff), 2, false, 'empty', 10)], [-1, 0, -1, 0], 'empty entries have no extent');
  assert.deepEqual([...parseShardIndex(raw, 2, false, 'unchecked')], [0, 50, 60, 30], 'no length given: no bounds check');
});
