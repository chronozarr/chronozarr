// The GLSL ES 3.00 that turns band values into a color, as one string any fragment shader can include.
//
//   vec4 shade(int product, vec3 x, float stretchLo)
//       x: the product's input bands as physical values (stored * scale + offset), in input order:
//          true color (red, green, blue), false color (nir, red, green), NDVI (nir, red), NDWI (green, nir),
//          water (green, nir), single band (the band, in x.x). Returns an opaque color. `product` is the
//          `shader` number of the product in products.js; `stretchLo` is the shadow-lift of the tone map.
//   vec4 shadeLinear(int product, vec3 x, vec2 range)
//       for values that are not reflectance: a straight stretch of [range.x, range.y] to 0..1, per channel for
//       the three-band products and to gray for the single band.
//
// Converting stored values to physical ones is left to the caller, so this signature does not depend on how the
// data is stored. The tone map must stay in step with reinhardSrgb in products.js.

import { GAIN } from './products.js';

export const PRODUCT_GLSL = `
const float GAIN = ${GAIN.toFixed(1)};
const float SAT_BOOST = 1.4;

float toSrgb(float v) {
  return v <= 0.0031308 ? 12.92 * v : 1.055 * pow(v, 1.0 / 2.4) - 0.055;
}

vec3 tone(vec3 refl, float lo) {
  vec3 sc = refl * GAIN;
  vec3 tm = sc / (sc + 1.0);
  vec3 sr = vec3(toSrgb(tm.r), toSrgb(tm.g), toSrgb(tm.b));
  float headroom = 1.0 - lo;
  if (headroom > 0.001) sr = clamp((sr - lo) / headroom, 0.0, 1.0);
  return sr;
}

vec3 trueColor(vec3 rgb, float lo) {
  vec3 sr = tone(rgb, lo);
  float lum = dot(sr, vec3(0.2126, 0.7152, 0.0722));
  return clamp(mix(vec3(lum), sr, SAT_BOOST), 0.0, 1.0);
}

vec3 ndviRamp(float v) {
  if (v < 0.0) return mix(vec3(0.14, 0.15, 0.19), vec3(0.48, 0.42, 0.36), clamp(v + 1.0, 0.0, 1.0));
  vec3 c0 = vec3(0.76, 0.70, 0.52);
  vec3 c1 = vec3(0.50, 0.76, 0.32);
  vec3 c2 = vec3(0.18, 0.52, 0.16);
  vec3 c3 = vec3(0.04, 0.28, 0.04);
  if (v < 0.2) return mix(c0, c1, v / 0.2);
  if (v < 0.5) return mix(c1, c2, (v - 0.2) / 0.3);
  return mix(c2, c3, clamp((v - 0.5) / 0.5, 0.0, 1.0));
}

vec3 ndwiRamp(float v) {
  if (v < 0.0) return mix(vec3(0.32, 0.26, 0.20), vec3(0.58, 0.52, 0.42), clamp(v + 1.0, 0.0, 1.0));
  vec3 c0 = vec3(0.62, 0.80, 0.94);
  vec3 c1 = vec3(0.22, 0.52, 0.84);
  vec3 c2 = vec3(0.06, 0.22, 0.52);
  if (v < 0.3) return mix(c0, c1, v / 0.3);
  return mix(c1, c2, clamp((v - 0.3) / 0.7, 0.0, 1.0));
}

vec4 shade(int product, vec3 x, float stretchLo) {
  if (product == 0) {
    return vec4(trueColor(x, stretchLo), 1.0);
  } else if (product == 1) {
    return vec4(tone(x, stretchLo), 1.0);
  } else if (product == 2) {
    float nir = x.x, red = x.y;
    return vec4(ndviRamp((nir + red) > 0.0 ? (nir - red) / (nir + red) : 0.0), 1.0);
  } else if (product == 3) {
    float green = x.x, nir = x.y;
    return vec4(ndwiRamp((green + nir) > 0.0 ? (green - nir) / (green + nir) : 0.0), 1.0);
  } else if (product == 4) {
    float green = x.x, nir = x.y;
    float ndwi = (green + nir) > 0.0 ? (green - nir) / (green + nir) : 0.0;
    if (ndwi > 0.0) return vec4(0.18, 0.48, 0.84, 1.0);
    float lum = tone(vec3(green), stretchLo).r;
    return vec4(vec3(lum * 0.65 + 0.06), 1.0);
  }
  return vec4(vec3(tone(vec3(x.x), stretchLo).r), 1.0);
}

vec4 shadeLinear(int product, vec3 x, vec2 range) {
  vec3 n = clamp((x - range.x) / (range.y - range.x), 0.0, 1.0);
  return product == 5 ? vec4(vec3(n.x), 1.0) : vec4(n, 1.0);
}
`;
