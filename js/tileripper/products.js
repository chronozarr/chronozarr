// Product definitions and the pure value math shared by the shader (via uniforms) and the sidebar.
// Products name their input bands; indices are resolved against the store's band list at load time.

export const REFLECTANCE_SCALE = 10000;

// `inputs` are in shader order: true color = (red, green, blue), NDVI = (nir, red), NDWI = (green, nir).
export const PRODUCTS = [
  { id: 'true_color', shader: 0, name: 'True color', inputs: ['B04', 'B03', 'B02'] },
  { id: 'false_color', shader: 1, name: 'False color', inputs: ['B08', 'B04', 'B03'] },
  { id: 'ndvi', shader: 2, name: 'NDVI', inputs: ['B08', 'B04'] },
  { id: 'ndwi', shader: 3, name: 'NDWI', inputs: ['B03', 'B08'] },
  { id: 'water', shader: 4, name: 'Water', inputs: ['B03', 'B08'] },
  { id: 'band', shader: 5, name: 'Single band', inputs: null },
];

/** Adds `available`, `missing` (band names) to each product for a store's band list. */
export function resolveProducts(bands) {
  return PRODUCTS.map((product) => {
    if (product.inputs === null) return { ...product, available: bands.length > 0, missing: [] };
    const missing = product.inputs.filter((name) => !bands.includes(name));
    return { ...product, available: missing.length === 0, missing };
  });
}

/** Band indices in shader input order (-1 = unused). `bandChoice` selects the band of the single-band product. */
export function inputIndices(product, bands, bandChoice) {
  const names = product.inputs ?? [bands[bandChoice]];
  const indices = [-1, -1, -1];
  names.forEach((name, i) => {
    indices[i] = bands.indexOf(name);
  });
  return indices;
}

export function ndvi(nir, red) {
  return nir + red > 0 ? (nir - red) / (nir + red) : null;
}

export function ndwi(green, nir) {
  return green + nir > 0 ? (green - nir) / (green + nir) : null;
}

/** Sidebar model for one pixel: per-band DN and reflectance plus indices when their bands exist. */
export function describePixel(values, bands) {
  const dn = (name) => {
    const i = bands.indexOf(name);
    return i < 0 ? null : values[i];
  };
  const [b03, b04, b08] = [dn('B03'), dn('B04'), dn('B08')];
  const ndviValue = b04 !== null && b08 !== null ? ndvi(b08, b04) : null;
  const ndwiValue = b03 !== null && b08 !== null ? ndwi(b03, b08) : null;
  return {
    bands: bands.map((name, i) => ({ name, dn: values[i], reflectance: values[i] / REFLECTANCE_SCALE })),
    ndvi: ndviValue,
    ndwi: ndwiValue,
    isWater: ndwiValue !== null && ndwiValue > 0,
    hasNdvi: b04 !== null && b08 !== null,
    hasNdwi: b03 !== null && b08 !== null,
  };
}

// Reinhard tone mapping, matching the fragment shader, used to pick the shadow-lift stretch.
export const GAIN = 5.0;
export function reinhardSrgb(reflectance) {
  const scaled = reflectance * GAIN;
  const t = scaled / (scaled + 1);
  return t <= 0.0031308 ? 12.92 * t : 1.055 * Math.pow(t, 1 / 2.4) - 0.055;
}

/** 2nd percentile of tone-mapped red/green/blue samples (DN triples); 0 with too few samples. */
export function computeStretchLo(rgbSamples) {
  const tones = [];
  for (const [r, g, b] of rgbSamples) {
    if (r === 0 && g === 0 && b === 0) continue;
    tones.push(reinhardSrgb(r / REFLECTANCE_SCALE), reinhardSrgb(g / REFLECTANCE_SCALE), reinhardSrgb(b / REFLECTANCE_SCALE));
  }
  if (tones.length < 30) return 0;
  tones.sort((a, b) => a - b);
  return tones[Math.floor(tones.length * 0.02)];
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
