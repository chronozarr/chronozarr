// The per-pixel time series behind the sidebar chart, as pure functions over already-decoded pixels.
//
// A reading is what was sampled at one timestep: `{ pixels }`, one Uint16Array of per-band DN for each
// pixel of a small window around the clicked pixel (the clicked pixel first). A timestep whose chunks are
// not loaded yet has no reading. A series has one value per timestep: a number, null where the pixel has no
// data (a gap), or undefined where the timestep is not loaded yet.

import { REFLECTANCE_SCALE, ndvi, ndwi } from './products.js';

const COLORS = { red: '#ef4444', green: '#34d399', blue: '#3b82f6', amber: '#f59e0b', grey: '#c5cbd9' };

/** Window (x, y) offsets around the clicked pixel, clicked pixel first, clipped to a width x height cell. */
export function windowPixels(x, y, width, height, radius) {
  const pixels = [[x, y]];
  const seen = new Set([`${x},${y}`]);
  for (let dy = -radius; dy <= radius; dy++) {
    for (let dx = -radius; dx <= radius; dx++) {
      const px = Math.min(width - 1, Math.max(0, x + dx));
      const py = Math.min(height - 1, Math.max(0, y + dy));
      if (!seen.has(`${px},${py}`)) {
        seen.add(`${px},${py}`);
        pixels.push([px, py]);
      }
    }
  }
  return pixels;
}

/**
 * What to chart for a product: one entry per line. `window` is the radius of the pixel window the readings
 * need (1 = 3x3 around the click), `domain` a fixed value range or null to scale to the data, and
 * `compute(pixels, nodata)` turns one reading's pixels into a number, or null for no data.
 */
export function seriesSpecs(product, bands, bandChoice) {
  const index = (name) => bands.indexOf(name);
  const reflectance = (name, label, color) => ({
    id: name,
    label,
    color,
    window: 0,
    domain: null,
    compute: ([pixel], nodata) => (pixel[index(name)] === nodata ? null : pixel[index(name)] / REFLECTANCE_SCALE),
  });
  switch (product.id) {
    case 'true_color':
      return [reflectance('B04', 'B04 red', COLORS.red), reflectance('B03', 'B03 green', COLORS.green), reflectance('B02', 'B02 blue', COLORS.blue)];
    case 'false_color':
      return [reflectance('B08', 'B08 nir', COLORS.red), reflectance('B04', 'B04 red', COLORS.green), reflectance('B03', 'B03 green', COLORS.blue)];
    case 'ndvi':
      return [{ id: 'ndvi', label: 'NDVI', color: COLORS.green, window: 0, domain: [-1, 1], compute: ([pixel]) => ndvi(pixel[index('B08')], pixel[index('B04')]) }];
    case 'ndwi':
      return [{ id: 'ndwi', label: 'NDWI', color: COLORS.blue, window: 0, domain: [-1, 1], compute: ([pixel]) => ndwi(pixel[index('B03')], pixel[index('B08')]) }];
    case 'water':
      return [
        {
          id: 'water',
          label: 'Water fraction (3×3)',
          color: COLORS.blue,
          window: 1,
          domain: [0, 1],
          compute: (pixels) => {
            const values = pixels.map((pixel) => ndwi(pixel[index('B03')], pixel[index('B08')])).filter((v) => v !== null);
            return values.length === 0 ? null : values.filter((v) => v > 0).length / values.length;
          },
        },
      ];
    default:
      return [reflectance(bands[bandChoice], `${bands[bandChoice]} reflectance`, COLORS.grey)];
  }
}

/** One value per timestep per spec: number, null (no data) or undefined (not loaded). */
export function buildSeries(specs, readings, nodata) {
  return specs.map((spec) => ({
    id: spec.id,
    label: spec.label,
    color: spec.color,
    domain: spec.domain,
    values: readings.map((reading) => (reading ? spec.compute(reading.pixels, nodata) : undefined)),
  }));
}

/** [lo, hi] for the y axis: the fixed domain if a series has one, else the data range with 8% padding. */
export function chartRange(series) {
  const fixed = series.find((s) => s.domain);
  if (fixed) return fixed.domain;
  const finite = series.flatMap((s) => s.values).filter((v) => typeof v === 'number');
  if (finite.length === 0) return [0, 1];
  const lo = Math.min(...finite);
  const hi = Math.max(...finite);
  const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.08 || 0.5;
  return [lo - pad, hi + pad];
}

/** SVG path for a series: a new segment after every gap (null); unloaded timesteps (undefined) are skipped. */
export function seriesPath(values, xOf, yOf) {
  let path = '';
  let drawing = false;
  values.forEach((value, t) => {
    if (value === undefined) return;
    if (value === null) {
      drawing = false;
      return;
    }
    path += `${drawing ? 'L' : 'M'}${xOf(t).toFixed(1)},${yOf(value).toFixed(1)}`;
    drawing = true;
  });
  return path;
}

/** x position of timestep t on an axis spanning [left, right] (a single timestep sits in the middle). */
export function xFromTime(t, count, left, right) {
  return count > 1 ? left + (t / (count - 1)) * (right - left) : (left + right) / 2;
}

/** The timestep nearest to x on the same axis, clamped to 0..count-1. */
export function timeFromX(x, count, left, right) {
  if (count <= 1) return 0;
  const frac = (x - left) / (right - left);
  return Math.min(count - 1, Math.max(0, Math.round(frac * (count - 1))));
}
