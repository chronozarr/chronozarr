// Colormaps for the single-band product. `gray` leaves the gray level as it is. The others are matplotlib's viridis,
// magma and turbo sampled at COLORMAP_STOPS evenly spaced points (sRGB, 0..1, three decimals) and interpolated
// linearly between them, which stays within 3.3/255 of matplotlib's 256-entry tables. The shader (products-glsl.js)
// and the legend in the viewer read the same tables, so what the legend shows is what the shader draws.

export const COLORMAP_STOPS = 33;

/** `stops` holds COLORMAP_STOPS r, g, b triples, flat; null for gray. */
export const COLORMAPS = [
  { id: 'gray', name: 'Gray', stops: null },
  {
    id: 'viridis',
    name: 'Viridis',
    stops: [
      0.267, 0.005, 0.329, 0.277, 0.05, 0.376, 0.282, 0.095, 0.417, 0.283, 0.136, 0.453, 0.279, 0.175, 0.483, 0.271, 0.214, 0.507, 0.259, 0.252, 0.525,
      0.245, 0.288, 0.537, 0.23, 0.322, 0.546, 0.214, 0.356, 0.551, 0.199, 0.388, 0.555, 0.186, 0.419, 0.557, 0.173, 0.449, 0.558, 0.161, 0.479, 0.558,
      0.149, 0.508, 0.557, 0.138, 0.537, 0.555, 0.128, 0.567, 0.551, 0.121, 0.596, 0.544, 0.121, 0.626, 0.533, 0.132, 0.655, 0.52, 0.158, 0.684, 0.502,
      0.197, 0.712, 0.479, 0.246, 0.739, 0.452, 0.304, 0.765, 0.42, 0.369, 0.789, 0.383, 0.44, 0.811, 0.341, 0.516, 0.831, 0.294, 0.596, 0.849, 0.243,
      0.678, 0.864, 0.19, 0.762, 0.876, 0.137, 0.846, 0.887, 0.1, 0.926, 0.897, 0.104, 0.993, 0.906, 0.144,
    ],
  },
  {
    id: 'magma',
    name: 'Magma',
    stops: [
      0.001, 0, 0.014, 0.014, 0.012, 0.069, 0.04, 0.031, 0.134, 0.074, 0.052, 0.203, 0.113, 0.065, 0.277, 0.159, 0.068, 0.353, 0.212, 0.062, 0.419,
      0.265, 0.06, 0.462, 0.317, 0.072, 0.485, 0.366, 0.09, 0.498, 0.415, 0.11, 0.505, 0.464, 0.13, 0.508, 0.513, 0.148, 0.508, 0.563, 0.165, 0.505,
      0.614, 0.182, 0.499, 0.665, 0.198, 0.489, 0.716, 0.215, 0.475, 0.767, 0.234, 0.458, 0.817, 0.256, 0.436, 0.863, 0.284, 0.412, 0.904, 0.32, 0.388,
      0.937, 0.365, 0.369, 0.961, 0.418, 0.36, 0.977, 0.476, 0.364, 0.987, 0.536, 0.382, 0.993, 0.595, 0.41, 0.996, 0.654, 0.446, 0.997, 0.712, 0.488,
      0.997, 0.77, 0.535, 0.995, 0.827, 0.586, 0.992, 0.884, 0.64, 0.989, 0.941, 0.698, 0.987, 0.991, 0.75,
    ],
  },
  {
    id: 'turbo',
    name: 'Turbo',
    stops: [
      0.19, 0.072, 0.232, 0.225, 0.164, 0.451, 0.251, 0.252, 0.634, 0.268, 0.338, 0.78, 0.276, 0.421, 0.891, 0.275, 0.501, 0.966, 0.259, 0.58, 0.999,
      0.214, 0.659, 0.98, 0.158, 0.736, 0.923, 0.112, 0.806, 0.845, 0.093, 0.866, 0.762, 0.12, 0.912, 0.687, 0.197, 0.949, 0.595, 0.305, 0.977, 0.49,
      0.428, 0.994, 0.386, 0.547, 0.999, 0.296, 0.644, 0.99, 0.234, 0.726, 0.965, 0.206, 0.805, 0.925, 0.205, 0.875, 0.873, 0.216, 0.933, 0.812, 0.227,
      0.973, 0.747, 0.225, 0.993, 0.674, 0.203, 0.996, 0.587, 0.169, 0.984, 0.493, 0.128, 0.958, 0.4, 0.088, 0.921, 0.315, 0.055, 0.874, 0.245, 0.033,
      0.816, 0.185, 0.018, 0.746, 0.131, 0.009, 0.664, 0.084, 0.004, 0.571, 0.045, 0.005, 0.48, 0.016, 0.011,
    ],
  },
];

/** Index of colormap `id` in COLORMAPS, or -1. The index is what the shader's colormap uniform takes. */
export function colormapIndex(id) {
  return COLORMAPS.findIndex((colormap) => colormap.id === id);
}

/** [r, g, b] (0..1) of COLORMAPS[index] at `v` (clamped to 0..1), interpolated as the shader does. */
export function colormapColor(index, v) {
  const t = Math.min(1, Math.max(0, v));
  const { stops } = COLORMAPS[index];
  if (!stops) return [t, t, t];
  const p = t * (COLORMAP_STOPS - 1);
  const i = Math.min(Math.floor(p), COLORMAP_STOPS - 2);
  const f = p - i;
  return [0, 1, 2].map((c) => stops[3 * i + c] * (1 - f) + stops[3 * (i + 1) + c] * f);
}

/** A left-to-right CSS linear-gradient of COLORMAPS[index], for a legend. */
export function colormapGradient(index) {
  const count = COLORMAPS[index].stops ? COLORMAP_STOPS : 2;
  const colors = Array.from({ length: count }, (_, i) => {
    const [r, g, b] = colormapColor(index, i / (count - 1)).map((c) => Math.round(c * 255));
    return `rgb(${r}, ${g}, ${b})`;
  });
  return `linear-gradient(to right, ${colors.join(', ')})`;
}
