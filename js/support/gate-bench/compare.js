// The verdict of the speed-gate job. run.mjs measures the viewer of the base commit and of the head commit on the same
// machine, in pairs of runs whose order alternates; this file decides from those runs whether the head is slower.
//
// A shared CI runner draws with SwiftShader on CPUs that other jobs use, so its timings are neither the gates' (which
// are for a real GPU) nor stable from one runner to the next. Within one job the two builds see the same machine, so the
// job compares them pair by pair: the median over the pairs of head / base must stay within the metric's tolerance, or
// the head median must be within `floorMs` of the base median (a 1 ms difference on a 2 ms step is not a regression a
// user sees). Wire bytes on a warm switch are not a timing and have no tolerance: any request or byte fails.

/**
 * The timed gates (repo CLAUDE.md, "Speed gates"). `gateMs` is the gate on a real GPU, shown next to the runner's figures.
 * The tolerances are the noise of the comparison on GitHub's runners, measured with the same build on both sides
 * (docs/evidence.md, "Speed gates in CI"), with room to spare. Decode is held tighter than its noise requires because its
 * gate has little headroom: 4.3 to 4.8 ms measured against 5 ms, so a 10 % slower decode would break the gate. Its floor is
 * below 5 % of the fastest runner's decode (5.5 ms), so that the floor does not loosen the tolerance there.
 */
export const METRICS = [
  { key: 'coldOpenMs', label: 'Cold open, level 0, 36 cells', gateMs: 500, tolerance: 0.15, floorMs: 20 },
  { key: 'warmStepMs', label: 'Warm switch, stepping', gateMs: 16, tolerance: 0.1, floorMs: 2 },
  { key: 'warmJumpMs', label: 'Warm switch, jumping', gateMs: 16, tolerance: 0.1, floorMs: 2 },
  { key: 'decodeMs', label: 'Decode per 2 MiB chunk', gateMs: 5, tolerance: 0.05, floorMs: 0.2 },
];

/**
 * The metrics of the compact pass (run.mjs --cache compact): the cold open and the warm switches on a cache that packs every chunk
 * in memory, as the public demo store gets (js/demo/cache-policy.js), against the same gates. Decode is the same code with or
 * without packing, so the default pass alone measures it.
 */
export const COMPACT_METRICS = METRICS.filter(({ key }) => key !== 'decodeMs').map((metric) => ({ ...metric, label: `${metric.label}, compact cache` }));

/** The marks of the first visit (first-visit.mjs): shown next to the gates, with no tolerance and no gate. */
export const FIRST_VISIT_MARKS = [
  { key: 'firstRequestMs', label: 'Navigation to the first chunk request' },
  { key: 'shaderMs', label: 'Shader compile and link, time blocked' },
  { key: 'workerReadyMs', label: 'Decode worker spawn to its first message, with its codec' },
  { key: 'codecMs', label: 'Codec module load by the page' },
  { key: 'firstFrameMs', label: 'Navigation to the first whole frame' },
  { key: 'targetFrameMs', label: 'Navigation to the frame at the target level' },
];

/** The counters of the warm loops that must all be 0 for the head. */
const TRAFFIC = ['serverRequests', 'readerRequests', 'readerBytes'];

export const MIN_PAIRS = 3;

export function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

export const stats = (values) => ({ median: median(values), min: Math.min(...values), max: Math.max(...values), n: values.length });

const sumLoops = (records) =>
  Object.fromEntries([...TRAFFIC, 'incomplete'].map((field) => [field, records.reduce((n, record) => n + record.warmLoop[field], 0)]));

/** The marks of the first visit of each build over the pairs; null when no run took them. A run whose visit failed has `firstVisit.error` and counts as unavailable. */
function firstVisitRows(pairs) {
  const visits = pairs.flatMap((pair) => [pair.base.firstVisit, pair.head.firstVisit]);
  if (!visits.some(Boolean)) return null;
  const rows = FIRST_VISIT_MARKS.map((mark) => {
    const values = (variant) => pairs.map((pair) => pair[variant].firstVisit?.[mark.key]).filter(Number.isFinite);
    const ratios = pairs.map((pair) => pair.head.firstVisit?.[mark.key] / pair.base.firstVisit?.[mark.key]).filter((ratio) => Number.isFinite(ratio) && ratio > 0);
    return { ...mark, base: values('base').length ? stats(values('base')) : null, head: values('head').length ? stats(values('head')) : null, ratio: ratios.length ? median(ratios) : null };
  });
  return { rows, unavailable: visits.filter((visit) => !visit || visit.error).length };
}

const formatMs = (ms) => (ms >= 100 ? `${Math.round(ms)}` : ms.toFixed(1));
const percent = (ratio) => `${ratio >= 1 ? '+' : '−'}${Math.round(Math.abs(ratio - 1) * 100)} %`;

/**
 * @param {{variant: 'base'|'head', pair: number, figures: Record<string, number>,
 *   warmLoop: {serverRequests: number, readerRequests: number, readerBytes: number, incomplete: number}}[]} records
 *   one per run: `figures` holds a value of every key of `metrics`, `warmLoop` what the warm loops of that run counted.
 */
export function compareBuilds(records, { metrics = METRICS } = {}) {
  const byPair = new Map();
  for (const record of records) {
    // A field that bench() no longer reports arrives as undefined, and NaN passes every comparison: stop instead.
    for (const { key } of metrics) {
      const value = record.figures[key];
      if (!(Number.isFinite(value) && value > 0)) {
        throw new Error(`${key} of the ${record.variant} run of pair ${record.pair + 1} is ${value}, not a time: did a field of bench() or of the warm loop change name?`);
      }
    }
    byPair.set(record.pair, { ...byPair.get(record.pair), [record.variant]: record });
  }
  const pairs = [...byPair.values()].filter((pair) => pair.base && pair.head);
  if (pairs.length < MIN_PAIRS) throw new Error(`${pairs.length} complete pairs of runs: comparing the builds needs at least ${MIN_PAIRS} pairs`);

  const rows = metrics.map((metric) => {
    const base = stats(pairs.map((pair) => pair.base.figures[metric.key]));
    const head = stats(pairs.map((pair) => pair.head.figures[metric.key]));
    const ratios = pairs.map((pair) => pair.head.figures[metric.key] / pair.base.figures[metric.key]);
    const ratio = median(ratios);
    const regression = ratio > 1 + metric.tolerance && head.median - base.median > metric.floorMs;
    return { ...metric, base, head, ratio, ratios, regression };
  });

  const traffic = { base: sumLoops(pairs.map((pair) => pair.base)), head: sumLoops(pairs.map((pair) => pair.head)) };
  const failures = rows
    .filter((row) => row.regression)
    .map((row) => `${row.label}: head ${formatMs(row.head.median)} ms, base ${formatMs(row.base.median)} ms; the head is ${percent(row.ratio)} over the median of ${pairs.length} pairs, more than the tolerance of +${Math.round(row.tolerance * 100)} %.`);
  const { serverRequests, readerRequests, readerBytes, incomplete } = traffic.head;
  if (serverRequests || readerRequests || readerBytes) {
    failures.push(`Warm switches of the head used the network: ${serverRequests} requests reached the data server, the reader counted ${readerRequests} requests and ${readerBytes} bytes, over ${pairs.length} runs. A warm switch must use none.`);
  }
  if (incomplete) {
    failures.push(`${incomplete} warm switches of the head drew an incomplete frame with every chunk of the loop in memory: those switches were not warm.`);
  }
  return { rows, traffic, pairs: pairs.length, failures, firstVisit: firstVisitRows(pairs) };
}

/** The step summary of the job: the verdict, the notice of the runs that were invalid for their load (guards.js: invalidRunsNotice) if there is one, then the absolute figures of both builds next to the gates. */
export function summaryMarkdown(result, { environment = [], title = 'Speed gates: head against base', invalid = '' } = {}) {
  const lines = [
    `## ${title}`,
    '',
    result.failures.length ? `**Fail.** ${result.failures.length === 1 ? 'One regression' : `${result.failures.length} regressions`}:` : '**Pass.** The head is not slower than the base beyond the tolerances, and its warm switches used no network.',
    ...result.failures.map((failure) => `- ${failure}`),
    ...(invalid ? ['', invalid] : []),
    '',
    `Medians over ${result.pairs} pairs of runs, with the range. Head / base is the median of the ratios of the pairs.`,
    '',
    '| Gate | Real-GPU gate | Base | Head | Head / base | Fails above |',
    '|---|---|---|---|---|---|',
    ...result.rows.map(
      (row) =>
        `| ${row.label} | ≤ ${row.gateMs} ms | ${formatMs(row.base.median)} ms (${formatMs(row.base.min)} to ${formatMs(row.base.max)}) | ${formatMs(row.head.median)} ms (${formatMs(row.head.min)} to ${formatMs(row.head.max)}) | ${row.ratio.toFixed(2)}${row.regression ? ' **fail**' : ''} | +${Math.round(row.tolerance * 100)} % and +${row.floorMs} ms |`,
    ),
    `| Wire bytes on a warm switch | 0 | ${trafficText(result.traffic.base)} | ${trafficText(result.traffic.head)} | | any |`,
    '',
    'The real-GPU gates are those of CLAUDE.md. A CI runner draws with SwiftShader (the WebGL renderer below) on shared CPUs, so its timings are compared with the base measured in the same job, not with the gates.',
  ];
  if (result.firstVisit) lines.push('', ...firstVisitLines(result.firstVisit));
  if (environment.length) lines.push('', ...environment.map((line) => `- ${line}`));
  return `${lines.join('\n')}\n`;
}

function firstVisitLines({ rows, unavailable }) {
  const cell = (stat) => (stat ? `${formatMs(stat.median)} ms (${formatMs(stat.min)} to ${formatMs(stat.max)})` : '–');
  return [
    '### First visit (informational, not gated)',
    '',
    'A new browser with an empty profile, once per run: the page, the viewer, its workers and the first open of the store, which the gates measure only after all of it is warm. The local server sends no cache headers, so the page and each worker download the codec themselves. Milliseconds from the start of the navigation, unless the row says otherwise; medians over the pairs, with the range.',
    '',
    '| Mark | Base | Head | Head / base |',
    '|---|---|---|---|',
    ...rows.map((row) => `| ${row.label} | ${cell(row.base)} | ${cell(row.head)} | ${row.ratio === null ? '–' : row.ratio.toFixed(2)} |`),
    ...(unavailable ? ['', `The first visit failed in ${unavailable} of the runs (\`firstVisit.error\` in the JSON); the rows use the others.`] : []),
  ];
}

function trafficText({ serverRequests, readerRequests, readerBytes, incomplete }) {
  const text = `${serverRequests} server requests, ${readerRequests} reader requests, ${readerBytes} bytes`;
  return incomplete ? `${text}; ${incomplete} incomplete frames` : text;
}
