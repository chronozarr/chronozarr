// Markdown tables from results/cog-vs-chronozarr.json (written by session.mjs).
//
//   node cog/report.mjs [results/cog-vs-chronozarr.json]

import { readFile } from 'node:fs/promises';
import path from 'node:path';

const file = process.argv[2] ?? path.join(import.meta.dirname, '../results/cog-vs-chronozarr.json');
const data = JSON.parse(await readFile(file, 'utf8'));
const REPS = [
  ['chronozarr', 'chronozarr'],
  ['cog', 'COG, geotiff.js defaults'],
  ['cogBlocked', 'COG, 64 KB blocks'],
];
const COGS = REPS.slice(1);

const kb = (bytes) => (bytes < 1e5 ? `${(bytes / 1e3).toFixed(1)} kB` : `${(bytes / 1e6).toFixed(bytes >= 1e8 ? 0 : 1)} MB`);
const ms = (value) => (value >= 10000 ? `${(value / 1000).toFixed(1)} s` : value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`);
const row = (cells) => `| ${cells.join(' | ')} |`;
const header = (cols) => [row(cols), `|${cols.map(() => '---').join('|')}|`];

const out = [];
out.push('### Requests and bytes', '');
out.push(...header(['phase', 'representation', 'header / index / metadata requests', 'header / index / metadata bytes', 'pixel requests', 'pixel bytes', 'total requests', 'total bytes']));
for (const phase of data.phases) {
  for (const [key, label] of REPS) {
    const own = data.requests[key].filter((r) => r.phase === phase.name);
    const side = own.filter((r) => r.role !== 'pixel');
    const pixel = own.filter((r) => r.role === 'pixel');
    const sum = (list) => list.reduce((n, r) => n + r.bytes, 0);
    out.push(row([phase.name, label, side.length, kb(sum(side)), pixel.length, kb(sum(pixel)), own.length, kb(sum(own))]));
  }
}

out.push('', '### Delivery time from the recorded requests', '');
const nat = data.links.natural;
out.push(`Links: ${Object.entries(data.links).map(([name, l]) => `${name} = ${l.mbps} Mbit/s, ${l.rttMs} ms`).join('; ')} (natural: medians of ${nat.probeRounds.length} probe rounds to the published store, observed ${nat.observedMbps.join(' to ')} Mbit/s and ${nat.observedRttMs.join(' to ')} ms). Parallel requests: COG ${data.parallel.cog}, chronozarr reader ${data.parallel.chronozarr}. ${data.requestOverheadBytes} bytes of headers per request.`, '');
const links = Object.keys(data.links);
for (const link of links) {
  const l = data.links[link];
  out.push(`**${link}** (${l.mbps} Mbit/s, ${l.rttMs} ms). Time, and in parentheses the ratio to chronozarr:`, '');
  out.push(...header(['phase', ...REPS.map(([, label]) => label)]));
  for (const phase of data.phases) {
    const z = data.model.chronozarr[link][phase.name].totalMs;
    const cells = [phase.name, ms(z)];
    for (const [key] of COGS) {
      const c = data.model[key][link][phase.name].totalMs;
      cells.push(`${ms(c)} (${(c / z).toFixed(2)}x)`);
    }
    out.push(row(cells));
  }
  out.push('');
}

out.push('### Per step (sequential phases): median step time', '');
out.push(...header(['phase', ...links.flatMap((l) => REPS.map(([, label]) => `${l}: ${label}`))]));
for (const phase of data.phases.filter((p) => p.sequential && p.steps > 1)) {
  const cells = [phase.name];
  for (const link of links) {
    for (const [key] of REPS) {
      const steps = [...data.model[key][link][phase.name].stepMs].sort((a, b) => a - b);
      cells.push(ms(steps[Math.floor(steps.length / 2)]));
    }
  }
  out.push(row(cells));
}

out.push('', '### Wall-clock on this machine, local range server (median over repetitions; includes decoding)', '');
out.push(...header(['phase', ...REPS.map(([, label]) => label), ...COGS.map(([, label]) => `${label} / chronozarr`)]));
for (const phase of data.phases) {
  const z = data.wallClock.chronozarr[phase.name].medianMs;
  const others = COGS.map(([key]) => data.wallClock[key][phase.name].medianMs);
  out.push(row([phase.name, ms(z), ...others.map(ms), ...others.map((c) => `${(c / z).toFixed(2)}x`)]));
}

const { sizes, overviewCheck: ov, pixelHistory } = data;
out.push('', '### Checks', '');
out.push(`- Pixel history (${pixelHistory.timesteps} timesteps x ${pixelHistory.bands} bands, level 0): identical to \`${pixelHistory.reference}\` for both readers: ${pixelHistory.identical}.`);
out.push(`- Level 1 of ${ov.date} (COG overview ${ov.cogShape.join(' x ')}, store level ${ov.storeShape.join(' x ')}), ${ov.pixelsCompared} values compared: ${(ov.equalFraction * 100).toFixed(2)} % identical, ${(ov.withinOneFraction * 100).toFixed(2)} % within 1, mean absolute difference ${ov.meanAbsDiff.toFixed(4)}, maximum ${ov.maxAbsDiff}.`);
out.push(`- Bytes on disk: store ${(sizes.storeBytes / 1e9).toFixed(2)} GB; ${sizes.cogFiles} COGs ${(sizes.cogBytes / 1e9).toFixed(2)} GB (${(sizes.cogBytes / sizes.cogFiles / 1e6).toFixed(1)} MB per date).`);
console.log(out.join('\n'));
