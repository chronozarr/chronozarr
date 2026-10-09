// Small statistics shared by the runner and the summary. Percentiles take the sorted value at floor(q * n), as the
// chronozarr benchmarks do (js/demo/perf.js), so numbers are comparable with theirs.

const sorted = (values) => [...values].filter((v) => v !== null && v !== undefined && Number.isFinite(v)).sort((a, b) => a - b);

/** Median; the mean of the two middle values for an even count. null for no values. */
export function median(values) {
  const s = sorted(values);
  if (s.length === 0) return null;
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

export function percentile(values, q) {
  const s = sorted(values);
  return s.length === 0 ? null : s[Math.min(s.length - 1, Math.floor(q * s.length))];
}

/** {n, median, p95, min, max} of a list; n counts the finite values. */
export function describe(values) {
  const s = sorted(values);
  return { n: s.length, median: median(s), p95: percentile(s, 0.95), min: s[0] ?? null, max: s.at(-1) ?? null };
}

/** The gaps between animation frames of an idle page are nominal when their median is near 16.7 ms (a 60 Hz display clock). */
export function rafIsNominal(gaps, { min, max }) {
  const m = median(gaps);
  return m !== null && m >= min && m <= max;
}

/** `value (min-max)` for a table cell: the median over rounds and the range of the rounds. */
export function spread(values, format) {
  const d = describe(values);
  if (d.n === 0) return 'n/a';
  return d.n === 1 ? format(d.median) : `${format(d.median)} (${format(d.min)}-${format(d.max)})`;
}
