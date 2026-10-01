// Product definitions and the pure value math shared by the shader (via uniforms), the chart and the sidebar.
//
// A store describes each band as {name, common_name?, scale, offset, units?}; the physical value is
// stored * scale + offset. Products ask for bands by common name (red, green, blue, nir, swir16, ...). A band
// matches a common name by its `common_name`, else by its `name` being that word, else, for stores that only
// carry band names, by the Sentinel-2 name for it (red = B04).

/** `inputs` of the shader, in order: true color = (red, green, blue), NDVI = (nir, red), NDWI = (green, nir). */
export const PRODUCTS = [
  { id: 'true_color', shader: 0, name: 'True color', needs: ['red', 'green', 'blue'] },
  { id: 'false_color', shader: 1, name: 'False color', needs: ['nir', 'red', 'green'] },
  { id: 'ndvi', shader: 2, name: 'NDVI', needs: ['nir', 'red'] },
  { id: 'ndwi', shader: 3, name: 'NDWI', needs: ['green', 'nir'] },
  { id: 'water', shader: 4, name: 'Water', needs: ['green', 'nir'] },
  { id: 'band', shader: 5, name: 'Single band', needs: null },
];

const SENTINEL2_NAMES = { blue: 'B02', green: 'B03', red: 'B04', nir: 'B08', swir16: 'B11', swir22: 'B12' };

/** Largest stored value of each dtype the viewer can show, to tell display-ready values from reflectance. */
const DTYPE_MAX = { uint8: 255, uint16: 65535 };

/** Sentinel-2 L2A reflectance scale, implied by the string form of `bands` in v0.1 stores. */
const LEGACY_SCALE = 1e-4;

/**
 * Band objects {name, common_name?, units?, scale, offset, divisor} from the store's band list (objects, or names,
 * which imply Sentinel-2 reflectance). `divisor` is 1 / scale when that is a whole number (10000 for 1e-4), else 0:
 * dividing by 10000 and multiplying by 1e-4 differ in the last bit, and stores written before band metadata must
 * render exactly as they did.
 */
export function normalizeBands(rawBands) {
  return rawBands.map((band) => {
    const source = typeof band === 'string' ? { name: band, scale: LEGACY_SCALE } : band;
    const scale = source.scale ?? 1;
    const inverse = 1 / scale;
    const divisor = scale !== 1 && Number.isInteger(Math.round(inverse)) && Math.abs(inverse - Math.round(inverse)) < 1e-6 * inverse ? Math.round(inverse) : 0;
    return { ...source, scale, offset: source.offset ?? 0, divisor };
  });
}

/** Physical value of a stored value, written to match the shader. */
export function toPhysical(stored, band) {
  return (band.divisor ? stored / band.divisor : stored * band.scale) + band.offset;
}

/** Index of the band that answers to a common name, or -1. */
export function findBand(bands, common) {
  let index = bands.findIndex((band) => band.common_name === common);
  if (index < 0) index = bands.findIndex((band) => band.common_name === undefined && band.name.toLowerCase() === common);
  const alias = SENTINEL2_NAMES[common];
  if (index < 0 && alias) index = bands.findIndex((band) => band.common_name === undefined && band.name === alias);
  return index;
}

/**
 * Each product with `available`, `missing` (common names no band answers to) and `indices`, the band index for
 * each input in shader order (-1 = none). The single-band product needs no particular band.
 */
export function resolveProducts(rawBands) {
  const bands = normalizeBands(rawBands);
  return PRODUCTS.map((product) => {
    if (product.needs === null) return { ...product, available: bands.length > 0, missing: [], indices: [0, -1, -1] };
    const indices = product.needs.map((common) => findBand(bands, common));
    const missing = product.needs.filter((_, i) => indices[i] < 0);
    return { ...product, available: missing.length === 0, missing, indices: [...indices, -1, -1, -1].slice(0, 3) };
  });
}

/** Band indices in shader input order (-1 = unused). `bandChoice` selects the band of the single-band product. */
export function inputIndices(product, bandChoice) {
  return product.needs === null ? [bandChoice, -1, -1] : product.indices;
}

/**
 * Per shader input: how a stored value becomes a physical one (`unitScale`, `unitDivisor`, `unitOffset`, three
 * numbers each; identity for unused inputs). Named apart from the camera's `scale` they are passed along with.
 */
export function inputConversion(product, bands, bandChoice) {
  const conversion = { unitScale: [1, 1, 1], unitDivisor: [0, 0, 0], unitOffset: [0, 0, 0] };
  inputIndices(product, bandChoice).forEach((index, slot) => {
    if (index < 0) return;
    conversion.unitScale[slot] = bands[index].scale;
    conversion.unitDivisor[slot] = bands[index].divisor;
    conversion.unitOffset[slot] = bands[index].offset;
  });
  return conversion;
}

/**
 * How the product's values reach the screen. `reflectance`: physical values of about 0..1, tone mapped with a
 * shadow-lift stretch (Sentinel-2 style). `linear`: a straight min/max stretch. `fixed` marks a range that comes
 * from the data type (8-bit color), not something to adjust. An 8-bit RGB store with scale 1 is display-ready and
 * shown as it is; a single band whose values are not reflectance-like (floats, signed integers, unscaled integers)
 * gets an adjustable linear stretch; indices and Sentinel-2 style bands keep the reflectance look.
 */
export function displayMode(product, bands, bandChoice, dtype) {
  const used = inputIndices(product, bandChoice).filter((i) => i >= 0).map((i) => bands[i]);
  if (product.id === 'true_color' || product.id === 'false_color') {
    const displayReady = dtype === 'uint8' && used.every((band) => band.scale === 1 && band.offset === 0);
    return displayReady ? { mode: 'linear', fixed: true, range: [0, 255] } : { mode: 'reflectance', fixed: true, range: null };
  }
  if (product.id !== 'band') return { mode: 'reflectance', fixed: true, range: null };
  const [band] = used;
  const max = DTYPE_MAX[dtype];
  const reflectanceLike = max !== undefined && toPhysical(max, band) <= 10;
  return reflectanceLike ? { mode: 'reflectance', fixed: true, range: null } : { mode: 'linear', fixed: false, range: null };
}

export function ndvi(nir, red) {
  return nir + red > 0 ? (nir - red) / (nir + red) : null;
}

export function ndwi(green, nir) {
  return green + nir > 0 ? (green - nir) / (green + nir) : null;
}

/** Whether a band is Sentinel-2 style reflectance: declared so, or (with no units) scaled by 1/10000 as v0.1 stores are. */
export function isReflectance(band) {
  return band.units === 'reflectance' || (band.units === undefined && band.divisor === 10000);
}

/**
 * Sidebar model for one pixel: per band the stored and physical values, plus indices when their bands exist. A band
 * at `nodata` counts as missing (so a pixel with no data has no indices).
 */
export function describePixel(values, rawBands, nodata = null) {
  const bands = normalizeBands(rawBands);
  const physical = (common) => {
    const i = findBand(bands, common);
    return i < 0 || values[i] === nodata ? null : toPhysical(values[i], bands[i]);
  };
  const [green, red, nir] = [physical('green'), physical('red'), physical('nir')];
  const hasNdvi = findBand(bands, 'red') >= 0 && findBand(bands, 'nir') >= 0;
  const hasNdwi = findBand(bands, 'green') >= 0 && findBand(bands, 'nir') >= 0;
  const ndviValue = red !== null && nir !== null ? ndvi(nir, red) : null;
  const ndwiValue = green !== null && nir !== null ? ndwi(green, nir) : null;
  return {
    bands: bands.map((band, i) => ({ name: band.name, units: band.units ?? null, reflectance: isReflectance(band), stored: values[i], value: toPhysical(values[i], band) })),
    ndvi: ndviValue,
    ndwi: ndwiValue,
    isWater: ndwiValue !== null && ndwiValue > 0,
    hasNdvi,
    hasNdwi,
  };
}

// Reinhard tone mapping, matching the fragment shader, used to pick the shadow-lift stretch.
export const GAIN = 5.0;
export function reinhardSrgb(reflectance) {
  const scaled = reflectance * GAIN;
  const t = scaled / (scaled + 1);
  return t <= 0.0031308 ? 12.92 * t : 1.055 * Math.pow(t, 1 / 2.4) - 0.055;
}

/** 2nd percentile of tone-mapped red/green/blue reflectance samples ([r, g, b] triples); 0 with too few samples. */
export function computeStretchLo(rgbSamples) {
  const tones = [];
  for (const [r, g, b] of rgbSamples) {
    if (r === 0 && g === 0 && b === 0) continue;
    tones.push(reinhardSrgb(r), reinhardSrgb(g), reinhardSrgb(b));
  }
  if (tones.length < 30) return 0;
  tones.sort((a, b) => a - b);
  return tones[Math.floor(tones.length * 0.02)];
}

/** [lo, hi] at the 2nd and 98th percentile of the finite values; null with none. Equal ends are spread by 1. */
export function percentileRange(values) {
  const finite = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (finite.length === 0) return null;
  const lo = finite[Math.floor(finite.length * 0.02)];
  const hi = finite[Math.min(finite.length - 1, Math.floor(finite.length * 0.98))];
  return hi > lo ? [lo, hi] : [lo, lo + 1];
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** "Mar 2024" when every timestep is the first of a month, otherwise the ISO date. */
export function makeTimeFormatter(times) {
  const monthly = times.every((s) => s.slice(8, 10) === '01');
  return (t) => {
    const s = times[t];
    return monthly ? `${MONTH_NAMES[Number(s.slice(5, 7)) - 1]} ${s.slice(0, 4)}` : s.slice(0, 10);
  };
}
