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

/** The counters of the warm loops that must all be 0 for the head. */
const TRAFFIC = ['serverRequests', 'readerRequests', 'readerBytes'];

export const MIN_PAIRS = 3;

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

const stats = (values) => ({ median: median(values), min: Math.min(...values), max: Math.max(...values), n: values.length });

const sumLoops = (records) =>
  Object.fromEntries([...TRAFFIC, 'incomplete'].map((field) => [field, records.reduce((n, record) => n + record.warmLoop[field], 0)]));

const formatMs = (ms) => (ms >= 100 ? `${Math.round(ms)}` : ms.toFixed(1));
const percent = (ratio) => `${ratio >= 1 ? '+' : '−'}${Math.round(Math.abs(ratio - 1) * 100)} %`;

/**
 * @param {{variant: 'base'|'head', pair: number, figures: Record<string, number>,
 *   warmLoop: {serverRequests: number, readerRequests: number, readerBytes: number, incomplete: number}}[]} records
 *   one per run: `figures` holds a value of every METRICS key, `warmLoop` what the warm loops of that run counted.
 */
export function compareBuilds(records) {
  const byPair = new Map();
  for (const record of records) {
    // A field that bench() no longer reports arrives as undefined, and NaN passes every comparison: stop instead.
    for (const { key } of METRICS) {
      const value = record.figures[key];
      if (!(Number.isFinite(value) && value > 0)) {
        throw new Error(`${key} of the ${record.variant} run of pair ${record.pair + 1} is ${value}, not a time: did a field of bench() or of the warm loop change name?`);
      }
    }
    byPair.set(record.pair, { ...byPair.get(record.pair), [record.variant]: record });
  }
  const pairs = [...byPair.values()].filter((pair) => pair.base && pair.head);
  if (pairs.length < MIN_PAIRS) throw new Error(`${pairs.length} complete pairs of runs: comparing the builds needs at least ${MIN_PAIRS} pairs`);

  const rows = METRICS.map((metric) => {
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
  return { rows, traffic, pairs: pairs.length, failures };
}

/** The step summary of the job: the verdict, then the absolute figures of both builds next to the gates. */
export function summaryMarkdown(result, { environment = [] } = {}) {
  const lines = [
    '## Speed gates: head against base',
    '',
    result.failures.length ? `**Fail.** ${result.failures.length === 1 ? 'One regression' : `${result.failures.length} regressions`}:` : '**Pass.** The head is not slower than the base beyond the tolerances, and its warm switches used no network.',
    ...result.failures.map((failure) => `- ${failure}`),
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
  if (environment.length) lines.push('', ...environment.map((line) => `- ${line}`));
  return `${lines.join('\n')}\n`;
}

function trafficText({ serverRequests, readerRequests, readerBytes, incomplete }) {
  const text = `${serverRequests} server requests, ${readerRequests} reader requests, ${readerBytes} bytes`;
  return incomplete ? `${text}; ${incomplete} incomplete frames` : text;
}
