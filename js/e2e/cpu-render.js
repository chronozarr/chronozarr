// A CPU version of what the fragment shader does to one texel, for comparing against the pixels the GPU drew. It
// follows renderer.js (the nodata rule, stored -> physical) and products-glsl.js (the color of each product); the
// tone map gain comes from products.js, which the shader's copy must stay in step with.

import { GAIN } from '../demo/products.js';

export const BACKGROUND = [0.035, 0.047, 0.071].map((c) => Math.round(c * 255));

const SAT_BOOST = 1.4;
const clamp01 = (v) => Math.min(1, Math.max(0, v));
const mix = (a, b, f) => a + (b - a) * f;
const mix3 = (a, b, f) => a.map((v, i) => mix(v, b[i], f));
const toSrgb = (v) => (v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055);

function tone(refl, lo) {
  const toned = refl.map((v) => {
    const scaled = v * GAIN;
    return toSrgb(scaled / (scaled + 1));
  });
  const headroom = 1 - lo;
  return headroom > 0.001 ? toned.map((v) => clamp01((v - lo) / headroom)) : toned;
}

function trueColor(rgb, lo) {
  const sr = tone(rgb, lo);
  const lum = 0.2126 * sr[0] + 0.7152 * sr[1] + 0.0722 * sr[2];
  return sr.map((v) => clamp01(mix(lum, v, SAT_BOOST)));
}

function ndviRamp(v) {
  if (v < 0) return mix3([0.14, 0.15, 0.19], [0.48, 0.42, 0.36], clamp01(v + 1));
  const c0 = [0.76, 0.7, 0.52];
  const c1 = [0.5, 0.76, 0.32];
  const c2 = [0.18, 0.52, 0.16];
  const c3 = [0.04, 0.28, 0.04];
  if (v < 0.2) return mix3(c0, c1, v / 0.2);
  if (v < 0.5) return mix3(c1, c2, (v - 0.2) / 0.3);
  return mix3(c2, c3, clamp01((v - 0.5) / 0.5));
}

function ndwiRamp(v) {
  if (v < 0) return mix3([0.32, 0.26, 0.2], [0.58, 0.52, 0.42], clamp01(v + 1));
  const c0 = [0.62, 0.8, 0.94];
  const c1 = [0.22, 0.52, 0.84];
  const c2 = [0.06, 0.22, 0.52];
  if (v < 0.3) return mix3(c0, c1, v / 0.3);
  return mix3(c1, c2, clamp01((v - 0.3) / 0.7));
}

const normalizedDifference = (a, b) => (a + b > 0 ? (a - b) / (a + b) : 0);

/** `shade` of products-glsl.js: x = the product's inputs as physical values. Returns r, g, b in 0..1. */
function shade(product, x, lo) {
  if (product === 0) return trueColor(x, lo);
  if (product === 1) return tone(x, lo);
  if (product === 2) return ndviRamp(normalizedDifference(x[0], x[1]));
  if (product === 3) return ndwiRamp(normalizedDifference(x[0], x[1]));
  if (product === 4) {
    if (normalizedDifference(x[0], x[1]) > 0) return [0.18, 0.48, 0.84];
    const lum = tone([x[0], x[0], x[0]], lo)[0] * 0.65 + 0.06;
    return [lum, lum, lum];
  }
  const gray = tone([x[0], x[0], x[0]], lo)[0];
  return [gray, gray, gray];
}

/** `shadeLinear`: a straight stretch of [range[0], range[1]], gray for the single band. */
function shadeLinear(product, x, range) {
  const n = x.map((v) => clamp01((v - range[0]) / (range[1] - range[0])));
  return product === 5 ? [n[0], n[0], n[0]] : n;
}

/**
 * The color of a texel as bytes.
 * @param {object} frame  what the viewer passed to Renderer.beginPaint for the frame (inputs, unit conversion, display, ...)
 * @param {number[]} stored  stored value of every band of the store at this texel
 * @returns {number[]} r, g, b as 0..255 (not rounded)
 */
export function shadeTexel(frame, stored) {
  const isNodata = (v) => (frame.nodata !== null && v === frame.nodata) || Number.isNaN(v);
  const inputs = frame.inputs.map((band) => (band < 0 ? null : stored[band]));
  if (inputs.every((v) => v === null || isNodata(v))) return [...BACKGROUND];
  const physical = inputs.map((v, slot) => {
    const value = v ?? 0;
    return (frame.unitDivisor[slot] > 0 ? value / frame.unitDivisor[slot] : value * frame.unitScale[slot]) + frame.unitOffset[slot];
  });
  const color = frame.display === 'linear' ? shadeLinear(frame.shader, physical, frame.range) : shade(frame.shader, physical, frame.stretchLo);
  return color.map((c) => c * 255);
}
