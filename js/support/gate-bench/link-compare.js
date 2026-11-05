// The figures of the link A/B (link.mjs) and the comparison of its two builds. link.mjs runs scenarios in a page whose link is
// throttled and returns what the page saw (the viewer's paint events, the inputs it was given, playback's statistics);
// this file turns that into the figures of a run and compares the figures of the base and the head pair by pair, with the
// ratio logic of compare.js: the median over the pairs of head / base, a metric flagged only when it is beyond its tolerance
// and its floor. Nothing here is a gate: the tolerances are provisional (no same-build noise has been measured for these
// metrics yet, see CONTRIBUTING.md), and a flag is a reason to look, not a verdict that fails a job.

import { MIN_PAIRS, median, stats } from './compare.js';

/** The links of the runs: CDP's download rate and the delay before each response (cdp.js). */
export const LINKS = {
  '50mbit-40ms': { mbps: 50, latencyMs: 40, label: '50 Mbit/s, 40 ms' },
  '8mbit-80ms': { mbps: 8, latencyMs: 80, label: '8 Mbit/s, 80 ms' },
};

/**
 * What a run does after the store is open and the link is throttled. A drag moves the pointer along the timeline one
 * timestep every `stepMs`, from the first timestep to the last; a jump presses the timeline half the time axis away;
 * playback plays for a fixed time at the requested speed.
 */
export const SCENARIOS = {
  drag40: { kind: 'drag', label: 'Drag the slider, one step every 40 ms', stepMs: 40 },
  drag150: { kind: 'drag', label: 'Drag the slider, one step every 150 ms', stepMs: 150 },
  jump: { kind: 'jump', label: 'Far jump, half the time axis' },
  play4: { kind: 'play', label: 'Play, 4 steps/s requested', stepsPerSecond: 4 },
  play30: { kind: 'play', label: 'Play, 30 steps/s requested', stepsPerSecond: 30 },
};

const TRAFFIC = [
  { key: 'requests', label: 'Requests to the store', unit: '', better: 'lower', tolerance: 0.1, floor: 5 },
  { key: 'wireMB', label: 'Wire bytes', unit: 'MB', better: 'lower', tolerance: 0.1, floor: 1 },
];

/**
 * `better` says which way is better ('lower', 'higher'; null: shown, never flagged), `tolerance` the relative change
 * of the median ratio, `floor` the change in the metric's own unit below which a ratio means nothing.
 */
export const LINK_METRICS = {
  drag: [
    { key: 'framesAtTarget', label: 'Frames at the target level', unit: '', better: 'higher', tolerance: 0.1, floor: 2 },
    { key: 'framesCoarser', label: 'Frames at a coarser level', unit: '', better: null },
    { key: 'held', label: 'Held: no whole frame ready', unit: '', better: 'lower', tolerance: 0.1, floor: 2 },
    { key: 'skipped', label: 'Timesteps never shown', unit: '', better: 'lower', tolerance: 0.1, floor: 2 },
    { key: 'lagMedianMs', label: 'Input to frame, median', unit: 'ms', better: 'lower', tolerance: 0.2, floor: 20 },
    { key: 'lagP95Ms', label: 'Input to frame, 95th percentile', unit: 'ms', better: 'lower', tolerance: 0.2, floor: 50 },
    { key: 'settleMs', label: 'Release to the frame at the target level', unit: 'ms', better: 'lower', tolerance: 0.2, floor: 200 },
    ...TRAFFIC,
  ],
  jump: [
    { key: 'firstFrameMs', label: 'First whole frame, any level', unit: 'ms', better: 'lower', tolerance: 0.2, floor: 100 },
    { key: 'targetFrameMs', label: 'Frame at the target level', unit: 'ms', better: 'lower', tolerance: 0.2, floor: 200 },
    ...TRAFFIC,
  ],
  play: [
    { key: 'steps', label: 'Steps taken', unit: '', better: 'higher', tolerance: 0.1, floor: 2 },
    { key: 'stepsPerSecond', label: 'Steps per second, first to last step', unit: '/s', better: 'higher', tolerance: 0.1, floor: 0.2 },
    { key: 'firstStepMs', label: 'Play to first step', unit: 'ms', better: 'lower', tolerance: 0.2, floor: 200 },
    { key: 'pauses', label: 'Buffering pauses', unit: '', better: 'lower', tolerance: 0.1, floor: 1 },
    { key: 'pausedMs', label: 'Time paused to buffer', unit: 'ms', better: 'lower', tolerance: 0.2, floor: 200 },
    ...TRAFFIC,
  ],
};

const round = (value, digits = 1) => Math.round(value * 10 ** digits) / 10 ** digits;

function quantile(values, q) {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
}

/**
 * The raw result of a drag, from the page: the first paint of every frame the viewer drew (`events`, each with its time `at`
 * and `t`, `lod`, `kind`), the `inputs` it was given (when each timestep was asked for), when the pointer was released and
 * the level the store shows at rest (`normalLod`). `capMs` is the time the page waited for the last frame after the release.
 */
export function dragFigures({ events, inputs, releaseAt, normalLod, from, to, capMs }) {
  const paints = events.filter((event) => event.type === 'paint');
  const duringDrag = paints.filter((paint) => paint.at <= releaseAt);
  const shownDuringDrag = new Set(duringDrag.map((paint) => paint.t).filter((t) => t !== from));
  // A step is answered by the first frame that shows its timestep or a later one, so a step that was skipped over waits for the next frame.
  const lags = [];
  for (const input of inputs) {
    const answer = paints.find((paint) => paint.at >= input.at && paint.t >= input.t);
    if (answer) lags.push(answer.at - input.at);
  }
  const settled = paints.find((paint) => paint.t === to && paint.lod === normalLod && paint.kind === 'target');
  const timedOut = [];
  if (!settled) timedOut.push('settleMs');
  if (!lags.length) timedOut.push('lagMs');
  const figures = {
    framesAtTarget: duringDrag.filter((paint) => paint.lod === normalLod).length,
    framesCoarser: duringDrag.filter((paint) => paint.lod > normalLod).length,
    held: events.filter((event) => event.type === 'kept' && event.at <= releaseAt).length,
    skipped: Math.abs(to - from) - shownDuringDrag.size,
    lagMedianMs: lags.length ? round(median(lags)) : capMs,
    lagP95Ms: lags.length ? round(quantile(lags, 0.95)) : capMs,
    settleMs: settled ? round(Math.max(0, settled.at - releaseAt)) : capMs,
  };
  return { figures, timedOut, detail: { normalLod, paintedDuringDrag: duringDrag.length, stepsNeverAnswered: inputs.length - lags.length } };
}

/** The raw result of a jump: when the timeline was pressed (`at`), the frames drawn since, the level at rest and the wait `capMs`. */
export function jumpFigures({ events, at, to, normalLod, capMs }) {
  const paints = events.filter((event) => event.type === 'paint' && event.at >= at && event.t === to);
  const first = paints[0];
  const target = paints.find((paint) => paint.lod === normalLod && paint.kind === 'target');
  const timedOut = [];
  if (!first) timedOut.push('firstFrameMs');
  if (!target) timedOut.push('targetFrameMs');
  const figures = { firstFrameMs: first ? round(first.at - at) : capMs, targetFrameMs: target ? round(target.at - at) : capMs };
  return { figures, timedOut, detail: { normalLod, firstFrameLod: first?.lod ?? null } };
}

/** The raw result of playback: its statistics after `windowMs` of playing, and the level it chose for the movie. */
export function playFigures({ stats: playback, startAt, windowMs, normalLod, movieLod }) {
  const timedOut = playback.firstStepAt === null ? ['firstStepMs'] : [];
  const playedMs = playback.lastStepAt - playback.firstStepAt;
  const figures = {
    steps: playback.steps,
    stepsPerSecond: playback.steps > 1 && playedMs > 0 ? round(((playback.steps - 1) / playedMs) * 1000, 2) : 0,
    firstStepMs: playback.firstStepAt === null ? windowMs : round(playback.firstStepAt - startAt),
    pauses: playback.bufferingPauses,
    pausedMs: round(playback.bufferingMs),
  };
  return { figures, timedOut, detail: { normalLod, movieLod, longestPauseMs: round(playback.longestBufferingMs), initialBufferMs: playback.initialBufferMs === null ? null : round(playback.initialBufferMs) } };
}

const FIGURES = { drag: dragFigures, jump: jumpFigures, play: playFigures };

/** The figures of one run: those of its scenario, and what the network log counted between the start of the scenario and its end. */
export function runFigures(scenario, raw, traffic) {
  const { figures, timedOut, detail } = FIGURES[SCENARIOS[scenario].kind](raw);
  return {
    figures: { ...figures, requests: traffic.requests, wireMB: round(traffic.bytes / 1e6) },
    timedOut,
    detail: { ...detail, failedRequests: traffic.failed },
  };
}

/** head / base of one pair; a pair whose base is 0 has no ratio unless the head is 0 too. */
function pairRatio(head, base) {
  if (base > 0) return head / base;
  return head === 0 ? 1 : null;
}

function verdictOf(metric, base, head, ratio) {
  if (!metric.better) return '';
  const up = ratio === null ? Infinity : ratio;
  const rise = head.median - base.median;
  const bigger = up > 1 + metric.tolerance && rise > metric.floor;
  const smaller = up < 1 - metric.tolerance && -rise > metric.floor;
  if (metric.better === 'lower') return bigger ? 'worse' : smaller ? 'better' : '';
  return smaller ? 'worse' : bigger ? 'better' : '';
}

/**
 * @param {{variant: 'base'|'head', pair: number, link: string, scenario: string, figures: Record<string, number>, timedOut: string[]}[]} records
 *   one per run, as link.mjs writes them.
 */
export function compareLinks(records) {
  const groups = new Map();
  for (const record of records) {
    const metrics = LINK_METRICS[SCENARIOS[record.scenario].kind];
    for (const { key } of metrics) {
      const value = record.figures[key];
      if (!(Number.isFinite(value) && value >= 0)) {
        throw new Error(`${key} of the ${record.variant} run of pair ${record.pair + 1} (${record.link}, ${record.scenario}) is ${value}, not a count or a time: did a field of the scenario change name?`);
      }
    }
    const id = `${record.link}/${record.scenario}`;
    const group = groups.get(id) ?? { link: record.link, scenario: record.scenario, pairs: new Map() };
    group.pairs.set(record.pair, { ...group.pairs.get(record.pair), [record.variant]: record });
    groups.set(id, group);
  }
  const timeouts = { base: 0, head: 0 };
  for (const record of records) timeouts[record.variant] += record.timedOut.length;

  const compared = [...groups.values()].map((group) => {
    const pairs = [...group.pairs.values()].filter((pair) => pair.base && pair.head);
    const decided = pairs.length >= MIN_PAIRS;
    const rows = LINK_METRICS[SCENARIOS[group.scenario].kind].map((metric) => {
      const base = stats(pairs.map((pair) => pair.base.figures[metric.key]));
      const head = stats(pairs.map((pair) => pair.head.figures[metric.key]));
      const ratios = pairs.map((pair) => pairRatio(pair.head.figures[metric.key], pair.base.figures[metric.key])).filter((ratio) => ratio !== null);
      // Pairs without a ratio are those whose base is 0 and head is not: the metric has no ratio only if every pair is like that.
      const ratio = ratios.length ? median(ratios) : null;
      return { ...metric, base, head, ratio, verdict: decided ? verdictOf(metric, base, head, ratio) : '' };
    });
    return { link: group.link, scenario: group.scenario, pairs: pairs.length, decided, rows };
  });
  return { groups: compared, timeouts, flagged: compared.flatMap((group) => group.rows.filter((row) => row.verdict === 'worse').map((row) => ({ link: group.link, scenario: group.scenario, key: row.key }))) };
}

const formatMs = (ms) => (ms >= 100 ? `${Math.round(ms)}` : ms.toFixed(1));
function formatValue(value, unit) {
  if (unit === 'ms') return `${formatMs(value)} ms`;
  const text = Number.isInteger(value) ? String(value) : value.toFixed(unit === '/s' ? 2 : 1);
  return unit ? `${text} ${unit}` : text;
}
const formatRange = (stat, unit) => `${formatValue(stat.median, unit)} (${formatValue(stat.min, unit)} to ${formatValue(stat.max, unit)})`;

/** The step summary of a run: for each link and scenario, both builds' medians with their range, head / base and the flags. */
export function linkSummaryMarkdown(result, { environment = [], records = [] } = {}) {
  const worse = result.flagged.length;
  const lines = [
    '## Link A/B: head against base',
    '',
    worse ? `**${worse} ${worse === 1 ? 'metric is' : 'metrics are'} worse in the head** beyond their tolerance and floor.` : 'No metric is worse in the head beyond its tolerance and floor.',
    '',
    'Medians over the pairs of runs, with the range. Head / base is the median of the ratios of the pairs. A flag needs the ratio beyond the tolerance and the change in the median beyond the floor; both are provisional, and a run with fewer than three pairs gets no flags.',
  ];
  const links = [...new Set(result.groups.map((group) => group.link))];
  for (const link of links) {
    lines.push('', `### ${LINKS[link].label}`);
    for (const group of result.groups.filter((candidate) => candidate.link === link)) {
      lines.push('', `**${SCENARIOS[group.scenario].label}** (${group.pairs} ${group.pairs === 1 ? 'pair' : 'pairs'})`, '', '| Metric | Base | Head | Head / base | |', '|---|---|---|---|---|');
      for (const row of group.rows) {
        const flag = row.verdict === 'worse' ? '**worse**' : row.verdict;
        lines.push(`| ${row.label} | ${formatRange(row.base, row.unit)} | ${formatRange(row.head, row.unit)} | ${row.ratio === null ? 'n/a' : row.ratio.toFixed(2)} | ${flag} |`);
      }
    }
  }
  const capped = result.timeouts.base + result.timeouts.head;
  if (capped) lines.push('', `${capped} ${capped === 1 ? 'figure is' : 'figures are'} the wait of the scenario, not a measurement (${result.timeouts.base} of the base, ${result.timeouts.head} of the head): the run did not show the frame or take the step in time. They are listed per run in the JSON as \`timedOut\`.`);
  const withErrors = records.filter((record) => record.errors?.length).length;
  if (withErrors) lines.push('', `${withErrors} runs logged errors in the page; they are in the JSON as \`errors\`.`);
  if (environment.length) lines.push('', ...environment.map((line) => `- ${line}`));
  return `${lines.join('\n')}\n`;
}
