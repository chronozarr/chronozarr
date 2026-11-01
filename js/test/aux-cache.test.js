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
