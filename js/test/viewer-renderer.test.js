import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Renderer, TEXTURE_FORMATS, fragmentShader } from '../demo/renderer.js';
import { packUint16 } from '../chronozarr/compact.js';
import { PRODUCT_GLSL } from '../shared/products-glsl.js';
import { GAIN, PRODUCTS } from '../shared/products.js';

test('the product GLSL defines shade() and shadeLinear() with stable signatures, and the tone map gain of products.js', () => {
  assert.match(PRODUCT_GLSL, /vec4 shade\(int product, vec3 x, float stretchLo\)/);
  assert.match(PRODUCT_GLSL, /vec4 shadeLinear\(int product, vec3 x, vec3 lo, vec3 hi\)/);
  assert.match(PRODUCT_GLSL, new RegExp(`const float GAIN = ${GAIN.toFixed(1)};`));
  assert.ok(PRODUCTS.every((p, i) => p.shader === i), 'shader numbers are the product positions the GLSL branches on');
});

test('each data type gets a fragment shader with its sampler, the shared product code, and direct true-value reads', () => {
  for (const [dtype, format] of Object.entries(TEXTURE_FORMATS)) {
    const source = fragmentShader(dtype);
    assert.ok(source.startsWith('#version 300 es'), dtype);
    assert.ok(source.includes(`uniform ${format.sampler} u_data;`), dtype);
    assert.ok(source.includes(PRODUCT_GLSL), `${dtype} includes the product code unchanged`);
    assert.match(source, /shade\(u_product, x, u_stretch_lo\)/);
    assert.doesNotMatch(source, /u_deltaBase|u_anchorBase|a \+ d/);
    assert.match(source, /u_dataBase \+ band/);
  }
  assert.match(fragmentShader('float32'), /isnan\(v\)/, 'NaN is nodata in float data');
  assert.doesNotMatch(fragmentShader('uint16'), /isnan/);
});

test('texture formats pair the GL internal format with upload types and a typed array of the same width', () => {
  assert.deepEqual(Object.keys(TEXTURE_FORMATS), ['uint8', 'uint16', 'int16', 'float32']);
  const widths = { uint8: 1, uint16: 2, int16: 2, float32: 4 };
  for (const [dtype, format] of Object.entries(TEXTURE_FORMATS)) {
    assert.equal(format.Array.BYTES_PER_ELEMENT, widths[dtype], dtype);
    assert.ok(format.internal.includes(String(widths[dtype] * 8)), `${dtype}: ${format.internal}`);
  }
});

test('the mask variant of each shader reads one mask layer per slot, writes the background where it is 0, and leaves the rest alone', () => {
  for (const dtype of Object.keys(TEXTURE_FORMATS)) {
    const plain = fragmentShader(dtype);
    const masked = fragmentShader(dtype, true);
    assert.doesNotMatch(plain, /u_mask/, `${dtype}: the plain shader has no mask`);
    assert.match(masked, /uniform usampler2DArray u_mask;/, dtype);
    assert.match(masked, /uniform int u_maskLayer;/, dtype);
    assert.match(masked, /texelFetch\(u_mask, ivec3\(texel, u_maskLayer\), 0\)\.r == 0u\)/, `${dtype}: mask 0 is invalid`);
    assert.match(masked, /outColor = vec4\(BG, 1\.0\);\s*return;\s*\}\s*float v0/, `${dtype}: invalid pixels get the opaque background before any data is read`);
    const withoutMask = masked.replace(/  if \(texelFetch\(u_mask[\s\S]*?\n  \}\n/, '').split('\n').filter((line) => !/u_mask/.test(line));
    assert.deepEqual(withoutMask.filter(Boolean), plain.split('\n').filter(Boolean), `${dtype}: apart from the mask lines the two programs are the same`);
  }
});

// A GL context that records what is handed to texSubImage3D, so an upload can be checked without a browser or GPU.
function recordingGl() {
  const constants = new Map();
  const uploads = [];
  const gl = new Proxy({}, {
    get(_, name) {
      if (name === 'getParameter') return () => 4096;
      if (name === 'getShaderParameter' || name === 'getProgramParameter') return () => true;
      if (name === 'getUniformLocation') return (_program, uniform) => uniform;
      if (name === 'texSubImage3D') return (...args) => uploads.push({ format: args[8], values: args[10] });
      if (/^create/.test(name)) return () => ({});
      if (/^[A-Z_0-9]+$/.test(name)) {
        if (!constants.has(name)) constants.set(name, constants.size + 1);
        return constants.get(name);
      }
      return () => {};
    },
  });
  return { gl, uploads };
}

// The words and blocks of a raw chunk that packUint16 declined, built one value at a time: two values per word, one 16-bit block of minimum 0 per 128 values.
function referenceRawPacked(data) {
  const words = new Uint32Array(Math.ceil(data.length / 2));
  for (let i = 0; i < data.length; i++) words[i >> 1] |= data[i] << ((i & 1) * 16);
  const blocks = new Uint32Array(Math.ceil(data.length / 128) * 2);
  for (let b = 0; b < blocks.length / 2; b++) {
    blocks[b * 2] = b * 64;
    blocks[b * 2 + 1] = 16 << 16;
  }
  return { words, blocks };
}

function compactRenderer(chunkWidth, chunkHeight) {
  const { gl, uploads } = recordingGl();
  const renderer = new Renderer({ getContext: () => gl });
  renderer.configure({ dtype: 'uint16', nBand: 1, chunkWidth, chunkHeight, slots: 8, compact: true });
  const uploaded = (format) => {
    const parts = uploads.filter((upload) => upload.format === gl[format]).map((upload) => upload.values);
    return { parts, all: Uint32Array.from(parts.flatMap((part) => [...part])) };
  };
  return { renderer, uploads, words: () => uploaded('RED_INTEGER'), blocks: () => uploaded('RG_INTEGER') };
}

function randomUint16(length, offsetBytes = 0) {
  let seed = 7 + length;
  const buffer = new ArrayBuffer(offsetBytes + length * 2);
  const data = new Uint16Array(buffer, offsetBytes, length);
  for (let i = 0; i < length; i++) {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    data[i] = seed >>> 16;
  }
  return data;
}

test('compact upload of a raw chunk sends the same words and blocks as the per-value packing, for even and odd lengths and an unaligned view', () => {
  const cases = [
    ['even length, 4-aligned', 64 * 128, 0],
    ['odd length', 63 * 65, 0],
    ['even length, view starting 2 bytes into its buffer', 64 * 128, 2],
    ['even length, 4-aligned view starting 8 bytes into its buffer', 64 * 128, 8],
    ['even length, partial last block', 70 * 74, 0],
  ];
  // One renderer takes every chunk, so the reused block table must also follow a change in length.
  const upload = compactRenderer(64, 128);
  for (const [name, length, offsetBytes] of cases) {
    const data = randomUint16(length, offsetBytes);
    assert.ok(data.some((value) => value >= 0x8000), `${name}: values with the top bit set are covered`);
    const { words, blocks } = referenceRawPacked(data);
    upload.uploads.length = 0;
    assert.ok(upload.renderer.upload(name, { lod: 0, row: 0, col: 0, t: 0 }, data) >= 0, name);
    assert.deepEqual(upload.words().all, words, `${name}: words`);
    assert.deepEqual(upload.blocks().all, blocks, `${name}: blocks`);
  }
});

test('compact upload of an even, 4-aligned raw chunk hands its own bytes to the GPU and reuses one block table', () => {
  const upload = compactRenderer(64, 128);
  const first = randomUint16(64 * 128);
  const second = randomUint16(64 * 128);
  upload.renderer.upload('a', { lod: 0, row: 0, col: 0, t: 0 }, first);
  upload.renderer.upload('b', { lod: 0, row: 0, col: 0, t: 1 }, second);
  const words = upload.words().parts;
  assert.ok(words.every((part) => part.buffer === first.buffer || part.buffer === second.buffer), 'the words are views of the decoded arrays, not copies');
  const blocks = upload.blocks().parts;
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].buffer, blocks[1].buffer, 'the block table of a raw chunk does not depend on its values');
});

test('compact upload of a packed chunk sends its own words and blocks', () => {
  const data = new Uint16Array(64 * 128).map((_, i) => 3000 + (i % 127));
  const packed = packUint16(data);
  assert.ok(packed);
  const upload = compactRenderer(64, 128);
  upload.renderer.upload('a', { lod: 0, row: 0, col: 0, t: 0 }, packed);
  assert.deepEqual(upload.words().all, packed.words);
  assert.deepEqual(upload.blocks().all, packed.blocks);
});
