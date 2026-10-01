// The per-pixel time series behind the sidebar chart, as pure functions over already-decoded pixels.
//
// A reading is what was sampled at one timestep: `{ pixels, valid? }`, one typed array of per-band stored values for
// each pixel of a small window around the clicked pixel (the clicked pixel first), and, for a store with a validity
// mask, `valid`: per pixel whether the mask of that timestep is 1 there. A timestep whose chunks are not loaded yet
// has no reading. A series has one value per timestep: a number, null where the pixel has no
// data (a gap), or undefined where the timestep is not loaded yet.

import { isReflectance, ndvi, ndwi, toPhysical } from './products.js';

// Custom properties of index.html (--series-*), so the lines and the legend follow the palette of the page: bright on the dark chrome, darker on the light one.
const COLORS = { red: 'var(--series-red)', green: 'var(--series-green)', blue: 'var(--series-blue)', amber: 'var(--series-amber)', grey: 'var(--series-grey)' };

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

/** Per window pixel (as from windowPixels): whether the validity mask (uint8 [y][x] over a chunk `chunkWidth` wide) is nonzero there. */
export function validAt(mask, chunkWidth, pixels) {
  return pixels.map(([x, y]) => mask[y * chunkWidth + x] !== 0);
}

/** A stored value that means "no data": the store's nodata value, or NaN in a float band. */
const isMissing = (value, nodata) => value === nodata || Number.isNaN(value);

/**
 * What to chart for a product: one entry per line. `window` is the radius of the pixel window the readings
 * need (1 = 3x3 around the click), `domain` a fixed value range or null to scale to the data, and
 * `compute(pixels, nodata)` turns one reading's pixels into a number, or null for no data. `bands` are the
 * store's band objects (see normalizeBands); values are physical (stored * scale + offset).
 */
export function seriesSpecs(product, bands, bandChoice) {
  const [i0, i1] = product.indices;
  const physical = (pixel, index, nodata) => (isMissing(pixel[index], nodata) ? null : toPhysical(pixel[index], bands[index]));
  const line = (index, role, color) => {
    const name = bands[index].name;
    return {
      id: name,
      label: name.toLowerCase() === role ? name : `${name} ${role}`,
      color,
      window: 0,
      domain: null,
      compute: ([pixel], nodata) => physical(pixel, index, nodata),
    };
  };
  // The two input bands of an index as physical values, or null when either is missing at this pixel.
  const pair = (pixel, nodata) => {
    const [first, second] = [physical(pixel, i0, nodata), physical(pixel, i1, nodata)];
    return first === null || second === null ? null : [first, second];
  };
  const index = (id, label, color, fn) => ({
    id,
    label,
    color,
    window: 0,
    domain: [-1, 1],
    compute: ([pixel], nodata) => {
      const values = pair(pixel, nodata);
      return values === null ? null : fn(...values);
    },
  });
  switch (product.id) {
    case 'true_color':
      return [line(product.indices[0], 'red', COLORS.red), line(product.indices[1], 'green', COLORS.green), line(product.indices[2], 'blue', COLORS.blue)];
    case 'false_color':
      return [line(product.indices[0], 'nir', COLORS.red), line(product.indices[1], 'red', COLORS.green), line(product.indices[2], 'green', COLORS.blue)];
    case 'ndvi':
      return [index('ndvi', 'NDVI', COLORS.green, ndvi)];
    case 'ndwi':
      return [index('ndwi', 'NDWI', COLORS.blue, ndwi)];
    case 'water':
      return [
        {
          id: 'water',
          label: 'Water fraction (3×3)',
          color: COLORS.blue,
          window: 1,
          domain: [0, 1],
          compute: (pixels, nodata) => {
            const values = pixels.map((pixel) => pair(pixel, nodata)).filter(Boolean).map(([green, nir]) => ndwi(green, nir)).filter((v) => v !== null);
            return values.length === 0 ? null : values.filter((v) => v > 0).length / values.length;
          },
        },
      ];
    default: {
      const band = bands[bandChoice];
      const label = isReflectance(band) ? `${band.name} reflectance` : band.units ? `${band.name} (${band.units})` : band.name;
      return [{ ...line(bandChoice, '', COLORS.grey), label }];
    }
  }
}

/**
 * The pixels of a reading a spec may use: those the mask marks valid (all of them without a mask). A spec on the
 * clicked pixel alone (window 0) needs that pixel valid, not a neighbour standing in for it.
 */
function usablePixels(reading, spec) {
  if (!reading.valid) return reading.pixels;
  if (spec.window === 0) return reading.valid[0] ? reading.pixels : [];
  return reading.pixels.filter((_, i) => reading.valid[i]);
}

/**
 * One value per timestep per spec: number, null (no data: the nodata value, or masked out) or undefined (not loaded).
 * With a validity mask, `nodata` is not compared (spec 2.3): pass null.
 */
export function buildSeries(specs, readings, nodata) {
  return specs.map((spec) => ({
    id: spec.id,
    label: spec.label,
    color: spec.color,
    domain: spec.domain,
    values: readings.map((reading) => {
      if (!reading) return undefined;
      const pixels = usablePixels(reading, spec);
      return pixels.length === 0 ? null : spec.compute(pixels, nodata);
    }),
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

/** Timesteps where the pixel was not observed (coverage 0) though the series has a value there: the points drawn hollow. */
export function gapFilledTimes(values, coverage) {
  if (!coverage) return [];
  return values.flatMap((value, t) => (coverage[t] === 0 && typeof value === 'number' ? [t] : []));
}
