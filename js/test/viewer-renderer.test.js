import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TEXTURE_FORMATS, fragmentShader } from '../tileripper/renderer.js';
import { PRODUCT_GLSL } from '../tileripper/products-glsl.js';
import { GAIN, PRODUCTS } from '../tileripper/products.js';

test('the product GLSL defines shade() and shadeLinear() with stable signatures, and the tone map gain of products.js', () => {
  assert.match(PRODUCT_GLSL, /vec4 shade\(int product, vec3 x, float stretchLo\)/);
  assert.match(PRODUCT_GLSL, /vec4 shadeLinear\(int product, vec3 x, vec2 range\)/);
  assert.match(PRODUCT_GLSL, new RegExp(`const float GAIN = ${GAIN.toFixed(1)};`));
  assert.ok(PRODUCTS.every((p, i) => p.shader === i), 'shader numbers are the product positions the GLSL branches on');
});

test('each data type gets a fragment shader with its sampler, the shared product code, and no other delta arithmetic', () => {
  for (const [dtype, format] of Object.entries(TEXTURE_FORMATS)) {
    const source = fragmentShader(dtype);
    assert.ok(source.startsWith('#version 300 es'), dtype);
    assert.ok(source.includes(`uniform ${format.sampler} u_data;`), dtype);
    assert.ok(source.includes(PRODUCT_GLSL), `${dtype} includes the product code unchanged`);
    assert.match(source, /shade\(u_product, x, u_stretch_lo\)/);
    assert.equal(source.includes('& ') && source.includes('(a + d)'), format.delta !== null, `${dtype}: only unsigned types add a delta`);
  }
  assert.match(fragmentShader('uint8'), /\(a \+ d\) & 255u/, 'modular residual at 8 bits');
  assert.match(fragmentShader('uint16'), /\(a \+ d\) & 65535u/, 'modular residual at 16 bits');
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
