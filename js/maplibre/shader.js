// GLSL for the MapLibre layer.
//
// Vertex: an origin-translated matrix (composed in float64 on the CPU) times a small mercator offset.
// Fragment: star-delta reconstruction (modular, spec v0.2) and the stored-to-physical conversion straight from the
// raw texture array, whatever the store's dtype; the product colors are PRODUCT_GLSL, shared with the viewer.
// Differences from js/tileripper/renderer.js: no background fill (no data is transparent so the basemap shows
// through), an optional validity mask, and opacity.

import { PRODUCT_GLSL } from '../tileripper/products-glsl.js';
import { TEXTURE_FORMATS } from '../tileripper/renderer.js';

export const VERTEX_SHADER = `#version 300 es
uniform mat4 u_matrix;   // clip <- mercator offset from the mesh origin
in vec2 a_pos;           // mercator offset from the mesh origin
in vec2 a_texel;         // texel inside the chunk
out vec2 v_texel;
void main() {
  gl_Position = u_matrix * vec4(a_pos, 0.0, 1.0);
  v_texel = a_texel;
}`;

/** The value of (texel, band) of the timestep on screen: the stored number, reconstructed from anchor + residual for star-delta. */
function valueGlsl({ delta }) {
  if (delta === null) {
    return `
float value(ivec2 texel, int band) {
  if (band < 0) return 0.0;
  return float(texelFetch(u_data, ivec3(texel, u_anchorBase + band), 0).r);
}`;
  }
  return `
// star-delta: value = (anchor + residual) mod 2^bits
float value(ivec2 texel, int band) {
  if (band < 0) return 0.0;
  uint a = texelFetch(u_data, ivec3(texel, u_anchorBase + band), 0).r;
  if (u_deltaBase < 0) return float(a);
  uint d = texelFetch(u_data, ivec3(texel, u_deltaBase + band), 0).r;
  return float((a + d) & ${delta}u);
}`;
}

/** Fragment shader for a store: `dtype` picks the sampler and the reconstruction; `hasMask` adds the validity mask texture. */
export function fragmentShader({ dtype, hasMask }) {
  const format = TEXTURE_FORMATS[dtype];
  if (!format) throw new Error(`chronozarr maplibre: no shader for data type ${dtype}; supported: ${Object.keys(TEXTURE_FORMATS).join(', ')}`);
  return `#version 300 es
precision highp float;
precision highp int;
precision highp ${format.sampler};
precision highp usampler2DArray;
in vec2 v_texel;
uniform ${format.sampler} u_data;
${hasMask ? 'uniform usampler2DArray u_mask;   // validity mask, one layer per slot: 1 = valid' : ''}
uniform vec2 u_extent;      // valid texels in the chunk
uniform int u_anchorBase;   // first layer of the anchor slot
uniform int u_deltaBase;    // first layer of the delta slot; -1 when the timestep is itself an anchor
uniform int u_maskLayer;    // layer of the mask slot (only read when the store has a mask)
uniform ivec3 u_inputs;     // band index per product input, -1 = unused
uniform int u_product;
uniform int u_display;      // 0 = tone-mapped reflectance, 1 = linear stretch of u_range
uniform vec2 u_range;
uniform float u_stretchLo;
uniform int u_hasNodata;    // the store declares a nodata value and has no mask
uniform float u_nodata;
uniform vec3 u_scale;       // per input: physical = stored * scale + offset ...
uniform vec3 u_divisor;     // ... or stored / divisor + offset where the divisor is above zero
uniform vec3 u_offset;
uniform float u_opacity;
out vec4 outColor;
${valueGlsl(format)}

bool isNodata(float v) {
  return (u_hasNodata != 0 && v == u_nodata)${dtype === 'float32' ? ' || isnan(v)' : ''};
}

vec3 toPhysical(vec3 v) {
  vec3 scaled = vec3(
    u_divisor.x > 0.0 ? v.x / u_divisor.x : v.x * u_scale.x,
    u_divisor.y > 0.0 ? v.y / u_divisor.y : v.y * u_scale.y,
    u_divisor.z > 0.0 ? v.z / u_divisor.z : v.z * u_scale.z);
  return scaled + u_offset;
}
${PRODUCT_GLSL}
void main() {
  ivec2 texel = ivec2(min(floor(v_texel), u_extent - 1.0));
${hasMask ? '  if (texelFetch(u_mask, ivec3(texel, u_maskLayer), 0).r == 0u) discard;   // masked out: the basemap shows through' : ''}
  float v0 = value(texel, u_inputs.x);
  float v1 = value(texel, u_inputs.y);
  float v2 = value(texel, u_inputs.z);
  bool empty = (u_inputs.x < 0 || isNodata(v0)) && (u_inputs.y < 0 || isNodata(v1)) && (u_inputs.z < 0 || isNodata(v2));
  if (empty) discard;                                              // no data: the basemap shows through
  vec3 x = toPhysical(vec3(v0, v1, v2));
  vec4 color = u_display == 1 ? shadeLinear(u_product, x, u_range) : shade(u_product, x, u_stretchLo);
  outColor = vec4(color.rgb * color.a * u_opacity, color.a * u_opacity);   // premultiplied, as MapLibre blends
}`;
}
