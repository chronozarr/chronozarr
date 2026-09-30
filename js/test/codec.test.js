import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import * as zarrita from 'zarrita';
import { createChunkDecoder } from '../chronozarr/codec.js';
import { crc32c, indexByteLength, parseShardIndex } from '../chronozarr/shard.js';

const FIXTURES = path.resolve(import.meta.dirname, '../../data/spike');
const skip = existsSync(path.join(FIXTURES, 'synthetic_sharded')) ? false : 'data/spike fixtures not present';

const BYTES_LE = { name: 'bytes', configuration: { endian: 'little' } };

test('crc32c matches the standard check value', () => {
  assert.equal(crc32c(new TextEncoder().encode('123456789')), 0xe3069283);
});

test('shard index: size, checksum and empty entries', () => {
  assert.equal(indexByteLength(3, true), 52);
  assert.equal(indexByteLength(3, false), 48);
  const raw = new DataView(new ArrayBuffer(16 * 2));
  raw.setBigUint64(0, 100n, true);
  raw.setBigUint64(8, 25n, true);
  raw.setBigUint64(16, 0xffffffffffffffffn, true);
  raw.setBigUint64(24, 0xffffffffffffffffn, true);
  const bytes = new Uint8Array(36);
  bytes.set(new Uint8Array(raw.buffer));
  new DataView(bytes.buffer).setUint32(32, crc32c(bytes.subarray(0, 32)), true);
  assert.deepEqual([...parseShardIndex(bytes, 2, true, 'test')], [100, 25, -1, 0]);
  bytes[3] ^= 1;
  assert.throws(() => parseShardIndex(bytes, 2, true, 'test'), /checksum mismatch/);
  assert.throws(() => parseShardIndex(bytes.subarray(0, 30), 2, true, 'test'), /shard index is 30 bytes, expected 36/);
});

for (const name of ['synthetic_sharded', 'synthetic_gzip']) {
  test(`chunk decoder reproduces expected.json band sums from raw ${name} bytes`, { skip }, async () => {
    const dir = path.join(FIXTURES, name);
    const meta = JSON.parse(readFileSync(path.join(dir, '0/data/zarr.json'), 'utf8'));
    const expected = JSON.parse(readFileSync(path.join(dir, 'expected.json'), 'utf8'))['0'];
    const sharding = meta.codecs[0].configuration;
    const decode = await createChunkDecoder(zarrita, { dtype: 'uint16', shape: sharding.chunk_shape, codecs: sharding.codecs });
    await decode.warmup();
    const [nTime, nBand, height, width] = meta.shape;
    const plane = 512 * 512;
    const sums = new Array(nBand).fill(0);
    for (let row = 0; row < 2; row++) {
      for (let col = 0; col < 2; col++) {
        const shard = new Uint8Array(readFileSync(path.join(dir, `0/data/c/0/0/${row}/${col}`)));
        const index = parseShardIndex(shard.subarray(shard.length - indexByteLength(nTime, true)), nTime, true, name);
        const chunk = await decode(shard.subarray(index[0], index[0] + index[1]));
        assert.equal(chunk.byteOffset, 0);
        assert.equal(chunk.buffer.byteLength, chunk.byteLength, 'result owns its buffer so a worker can transfer it');
        const h = Math.min(512, height - row * 512);
        const w = Math.min(512, width - col * 512);
        for (let b = 0; b < nBand; b++) for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) sums[b] += chunk[b * plane + y * 512 + x];
      }
    }
    assert.deepEqual(sums, expected.sum_per_t_b[0]);
  });
}

test('unsupported codecs and layouts are rejected with the reason', async () => {
  const shape = [1, 1, 2, 2];
  await assert.rejects(createChunkDecoder(zarrita, { dtype: 'float32', shape, codecs: [BYTES_LE] }), /unsupported chunk dtype float32/);
  await assert.rejects(createChunkDecoder(zarrita, { dtype: 'uint16', shape, codecs: [{ name: 'bytes', configuration: { endian: 'big' } }] }), /little-endian/);
  await assert.rejects(createChunkDecoder(zarrita, { dtype: 'uint16', shape, codecs: [BYTES_LE, { name: 'transpose', configuration: {} }] }), /unsupported codec "transpose"/);
});

test('a decoded chunk of the wrong size is an error', async () => {
  const fake = { registry: new Map([['gzip', () => ({ fromConfig: () => ({ decode: async () => new Uint8Array(6) }) })]]) };
  const decode = await createChunkDecoder(fake, { dtype: 'uint16', shape: [1, 1, 2, 2], codecs: [BYTES_LE, { name: 'gzip', configuration: {} }] });
  await assert.rejects(decode(new Uint8Array(1)), /decoded chunk is 6 bytes, expected 8/);
});

test('unaligned or shared decoder output is copied into an owned, aligned array', async () => {
  const backing = new Uint8Array(20);
  backing.set([1, 0, 2, 0, 3, 0, 4, 0], 5);
  const fake = { registry: new Map([['gzip', () => ({ fromConfig: () => ({ decode: async () => backing.subarray(5, 13) }) })]]) };
  const decode = await createChunkDecoder(fake, { dtype: 'uint16', shape: [1, 1, 2, 2], codecs: [BYTES_LE, { name: 'gzip', configuration: {} }] });
  const out = await decode(new Uint8Array(1));
  assert.deepEqual([...out], [1, 2, 3, 4]);
  assert.equal(out.buffer.byteLength, 8);
});
