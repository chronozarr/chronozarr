import { test } from 'node:test';
import assert from 'node:assert/strict';
import { packAux, unpackAux } from '../chronozarr/aux-cache.js';
import { ChunkCache } from '../chronozarr/cache.js';

test('compact auxiliaries preserve uniform, mixed binary, and arbitrary uint8 values exactly', () => {
  const arrays = [new Uint8Array(1027), new Uint8Array(1027).fill(1), new Uint8Array(1027).fill(255), Uint8Array.from({ length: 1027 }, (_, i) => i % 2), Uint8Array.from({ length: 1027 }, (_, i) => i % 256)];
  for (const data of arrays) assert.deepEqual(unpackAux(packAux(data), data.length), data);
  assert.equal(packAux(arrays[1]).length, 2);
  assert.equal(packAux(arrays[3]).length, 130, 'partial final byte is retained');
});

test('mixed masks survive decoded eviction across repeated full loops within the same bounded auxiliary budget', () => {
  const cache = new ChunkCache({ totalBytes: 1024, decodedBytes: 512, compressedBytes: 512, score: (entry) => -entry.used });
  const originals = Array.from({ length: 12 }, (_, t) => Uint8Array.from({ length: 256 }, (_, i) => (i + t) % 3 === 0 ? 0 : 1));
  let fetches = 0;
  const read = (t) => {
    let data = cache.decoded(String(t));
    if (data) return data;
    const compressed = cache.get(String(t))?.compressed;
    data = compressed ? unpackAux(compressed, 256) : originals[t];
    if (!compressed) fetches++;
    cache.insert(String(t), { t }, { data, compressed: compressed ? null : packAux(data) });
    assert.ok(cache.usedBytes <= 1024);
    return data;
  };
  for (let loop = 0; loop < 4; loop++) for (let t = 0; t < originals.length; t++) assert.deepEqual(read(t), originals[t]);
  assert.equal(fetches, originals.length, 'decoded eviction never refetches a retained mixed mask');
  assert.ok(cache.evictions.decoded > 0);
  cache.setBudgets({ totalBytes: 64, decodedBytes: 32, compressedBytes: 32 });
  assert.ok(cache.usedBytes <= 64, 'resizing promptly frees compact and decoded storage');
});

// The byte-at-a-time algorithm that packAux replaced, kept as the reference for its output.
function referencePackAux(data) {
  let uniform = true;
  let binary = true;
  for (let i = 0; i < data.length; i++) {
    if (data[i] !== data[0]) uniform = false;
    if (data[i] > 1) binary = false;
  }
  if (uniform) return Uint8Array.of(0, data[0] ?? 0);
  if (!binary) {
    const packed = new Uint8Array(data.length + 1);
    packed[0] = 2;
    packed.set(data, 1);
    return packed;
  }
  const packed = new Uint8Array(1 + Math.ceil(data.length / 8));
  packed[0] = 1;
  for (let i = 0; i < data.length; i++) packed[1 + (i >> 3)] |= data[i] << (i & 7);
  return packed;
}

// Node's deepEqual diffs failing typed arrays element by element, which takes minutes on a full chunk: find the first difference instead.
function assertSameBytes(actual, expected, message) {
  assert.equal(actual.length, expected.length, `${message}: length`);
  for (let i = 0; i < expected.length; i++) if (actual[i] !== expected[i]) assert.equal(actual[i], expected[i], `${message}: byte ${i}`);
}

test('packAux output is byte-identical to the byte-at-a-time packing for every length, alignment and kind of mask', () => {
  let seed = 12345;
  const random = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 2 ** 32;
  };
  const kinds = {
    zeros: () => 0,
    ones: () => 1,
    constant: () => 7,
    binary: () => (random() < 0.5 ? 1 : 0),
    'binary with a single one': (i) => (i === 13 ? 1 : 0),
    'binary with a single zero': (i) => (i === 13 ? 0 : 1),
    counts: (i) => i % 7,
    'one byte off an otherwise uniform array': (i) => (i === 21 ? 2 : 1),
    random: () => Math.floor(random() * 256),
  };
  for (const [kind, value] of Object.entries(kinds)) {
    // Lengths on both sides of every word and byte boundary. Offsets 1 to 3 give views that are not 4-byte aligned;
    // 4 and 8 give aligned views that do not start at the start of their buffer.
    for (const length of [...Array(41).keys(), 1027, 4096]) {
      for (const offset of [0, 1, 2, 3, 4, 8]) {
        const buffer = new Uint8Array(offset + length + 3);
        for (let i = 0; i < buffer.length; i++) buffer[i] = value(i - offset);
        const data = buffer.subarray(offset, offset + length);
        const before = data.slice();
        const packed = packAux(data);
        assertSameBytes(packed, referencePackAux(data), `${kind}, length ${length}, offset ${offset}`);
        assert.deepEqual(data, before, 'the input is not modified');
        assert.deepEqual(unpackAux(packed, length), data, `${kind}, length ${length}, offset ${offset}: round trip`);
      }
    }
  }
});

test('packAux matches the byte-at-a-time packing on full 512 x 512 masks', () => {
  const n = 512 * 512;
  const masks = [
    new Uint8Array(n).fill(1),
    new Uint8Array(n),
    Uint8Array.from({ length: n }, (_, i) => ((i % 512) + Math.floor(i / 512) * 0.7 > 400 ? 1 : 0)),
    Uint8Array.from({ length: n }, (_, i) => i % 7),
  ];
  for (const [i, mask] of masks.entries()) assertSameBytes(packAux(mask), referencePackAux(mask), `mask ${i}`);
});
