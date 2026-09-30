import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import * as zarrita from '../vendor/zarrita/index.js';
import { createChunkDecoder } from '../chronozarr/codec.js';

const BYTES_LE = { name: 'bytes', configuration: { endian: 'little' } };
const tiny = JSON.parse(readFileSync(path.resolve(import.meta.dirname, '../support/fixtures/blosc-tiny.json'), 'utf8'));
const fromBase64 = (text) => new Uint8Array(Buffer.from(text, 'base64'));

// 2 x 8 x 8 uint16 values compressed by python numcodecs Blosc (clevel 1 for zstd, 5 for lz4).
for (const variant of ['zstd_shuffle', 'zstd_noshuffle', 'lz4_shuffle', 'lz4_noshuffle']) {
  test(`blosc ${variant.replace('_', ', ')} decodes bit-exactly`, async () => {
    const [cname, shuffle] = variant.split('_');
    const decode = await createChunkDecoder(zarrita, {
      dtype: 'uint16',
      shape: [1, 2, 8, 8],
      codecs: [BYTES_LE, { name: 'blosc', configuration: { cname, clevel: 1, shuffle, typesize: 2, blocksize: 0 } }],
    });
    await decode.warmup();
    const out = await decode(fromBase64(tiny[variant]));
    assert.ok(out instanceof Uint16Array);
    assert.deepEqual([...out], tiny.values);
    assert.equal(out.byteOffset, 0);
    assert.equal(out.buffer.byteLength, out.byteLength, 'result owns its buffer so a worker can transfer it');
  });
}

test('blosc frames decode whatever the configuration says: the frame describes itself', async () => {
  const decode = await createChunkDecoder(zarrita, { dtype: 'uint16', shape: [1, 2, 8, 8], codecs: [BYTES_LE, { name: 'blosc', configuration: {} }] });
  assert.deepEqual([...(await decode(fromBase64(tiny.lz4_shuffle)))], tiny.values);
});

test('every supported dtype decodes to its typed array', async () => {
  const cases = [
    ['uint8', Uint8Array, Uint8Array.of(1, 2, 250, 255)],
    ['uint16', Uint16Array, Uint16Array.of(1, 65535, 300, 0)],
    ['int16', Int16Array, Int16Array.of(-32768, 32767, -1, 0)],
    ['float32', Float32Array, Float32Array.of(-1.5, 0.25, 1e7, NaN)],
  ];
  for (const [dtype, Type, values] of cases) {
    const identity = { registry: new Map([['gzip', () => ({ fromConfig: () => ({ decode: async (b) => b }) })]]) };
    const decode = await createChunkDecoder(identity, { dtype, shape: [1, 1, 2, 2], codecs: [BYTES_LE, { name: 'gzip', configuration: {} }] });
    const out = await decode(new Uint8Array(values.buffer.slice(0)));
    assert.ok(out instanceof Type, dtype);
    assert.deepEqual([...out].map(String), [...values].map(String), dtype);
  }
});

test('one-byte dtypes do not need an endian setting; wider ones must be little endian', async () => {
  const identity = { registry: new Map([['gzip', () => ({ fromConfig: () => ({ decode: async (b) => b }) })]]) };
  const gzip = { name: 'gzip', configuration: {} };
  const decode = await createChunkDecoder(identity, { dtype: 'uint8', shape: [1, 1, 2, 2], codecs: [{ name: 'bytes' }, gzip] });
  assert.deepEqual([...(await decode(Uint8Array.of(9, 8, 7, 6)))], [9, 8, 7, 6]);
  await assert.rejects(createChunkDecoder(identity, { dtype: 'float32', shape: [1, 1, 1, 1], codecs: [{ name: 'bytes', configuration: { endian: 'big' } }, gzip] }), /little-endian/);
  await assert.rejects(createChunkDecoder(identity, { dtype: 'int16', shape: [1, 1, 1, 1], codecs: [{ name: 'bytes' }, { name: 'bytes' }] }), /unsupported codec "bytes"/);
});

test('a decoded chunk of the wrong size names the dtype-sized expectation', async () => {
  const fake = { registry: new Map([['gzip', () => ({ fromConfig: () => ({ decode: async () => new Uint8Array(6) }) })]]) };
  const decode = await createChunkDecoder(fake, { dtype: 'float32', shape: [1, 1, 2, 2], codecs: [BYTES_LE, { name: 'gzip', configuration: {} }] });
  await assert.rejects(decode(new Uint8Array(1)), /decoded chunk is 6 bytes, expected 16/);
});

// Real chunks from the codec benchmark stores (data/spike/codec_bench, made by js/support/codec-bench/make_stores.py).
const BENCH = path.resolve(import.meta.dirname, '../../data/spike/codec_bench');
const skipBench = existsSync(path.join(BENCH, 'blosc')) ? false : 'data/spike/codec_bench not present';

for (const variant of ['zstd5', 'blosc']) {
  test(`real Ucayali chunks: ${variant} is bit-exact against the plain array`, { skip: skipBench }, async () => {
    const meta = JSON.parse(readFileSync(path.join(BENCH, variant, 'zarr.json'), 'utf8'));
    const decode = await createChunkDecoder(zarrita, { dtype: 'uint16', shape: [1, 4, 512, 512], codecs: meta.codecs });
    for (let c = 0; c < 4; c++) {
      const read = (name) => new Uint8Array(readFileSync(path.join(BENCH, name, 'c', String(c), '0/0/0')));
      const plain = new Uint16Array(read('plain').buffer);
      const out = await decode(read(variant));
      assert.equal(out.length, plain.length);
      assert.ok(out.every((v, i) => v === plain[i]), `chunk ${c}`);
    }
  });
}
