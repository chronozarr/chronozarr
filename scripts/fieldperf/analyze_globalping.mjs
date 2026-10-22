// Summarise the Globalping samples: per tag set (datacenter / eyeball probes), object kind and region, the samples are
// split by the cf-cache-status the response carried (HIT, or anything else), not by what each request was meant to be.
// p50 and p95 with percentile-bootstrap 95 % CIs (10,000 resamples) for dns, tcp, tls, first byte, download and total.
//
//   node scripts/fieldperf/analyze_globalping.mjs [--md]   -> OUT_DIR/globalping_summary.json (+ markdown on stdout with --md)

import fs from 'node:fs';
import path from 'node:path';
import { OUT_DIR, readJsonl, summarize } from './lib.mjs';

const md = process.argv.includes('--md');
const files = fs.readdirSync(OUT_DIR).filter((f) => /^globalping_(chunks|meta|plain)_.*\.jsonl$/.test(f));
const METRICS = ['dns', 'tcp', 'tls', 'firstByte', 'download', 'total'];
const REGION_ORDER = ['ashburn', 'frankfurt', 'sao_paulo', 'singapore'];

const rows = files.flatMap((f) => readJsonl(path.join(OUT_DIR, f)));
const ok = rows.filter((r) => r.statusCode === 200 && r.timings);
const out = { generatedAt: new Date().toISOString(), files, totalSamples: rows.length, usable: ok.length, failed: rows.length - ok.length, groups: [] };

const keyOf = (r) => `${r.tagSet}|${r.kind}|${r.region}`;
const groups = new Map();
for (const r of ok) {
  if (!groups.has(keyOf(r))) groups.set(keyOf(r), []);
  groups.get(keyOf(r)).push(r);
}

const tally = (arr, f) => arr.reduce((m, r) => ((m[f(r) ?? 'none'] = (m[f(r) ?? 'none'] ?? 0) + 1), m), {});

for (const [key, arr] of [...groups.entries()].sort()) {
  const [tagSet, kind, region] = key.split('|');
  const classes = { HIT: arr.filter((r) => r.cfCacheStatus === 'HIT'), 'non-HIT': arr.filter((r) => r.cfCacheStatus !== 'HIT') };
  const g = {
    tagSet,
    kind,
    region,
    samples: arr.length,
    cfMix: tally(arr, (r) => r.cfCacheStatus),
    cfMixByIntent: Object.fromEntries(['cold', 'warm', 'natural'].map((i) => [i, tally(arr.filter((r) => r.intent === i), (r) => r.cfCacheStatus)]).filter(([, v]) => Object.keys(v).length)),
    colos: tally(arr, (r) => r.colo),
    probes: { distinct: new Set(arr.map((r) => `${r.probe.asn}|${r.probe.city}|${r.probe.network}`)).size, networks: tally(arr, (r) => r.probe.network) },
    bytes: { contentLengthMedian: median(arr.map((r) => r.contentLength).filter(Number.isFinite)) },
    classes: {},
  };
  for (const [cls, list] of Object.entries(classes)) {
    g.classes[cls] = { n: list.length, metrics: {} };
    for (const m of METRICS) {
      const values = list.map((r) => r.timings[m]).filter((x) => Number.isFinite(x));
      g.classes[cls].metrics[m] = values.length >= 2 ? summarize(values, { seed: 4242 }) : { n: values.length };
    }
  }
  out.groups.push(g);
}
fs.writeFileSync(path.join(OUT_DIR, 'globalping_summary.json'), `${JSON.stringify(out, null, 1)}\n`);

function median(values) {
  const s = [...values].sort((a, b) => a - b);
  return s.length ? s[Math.floor(s.length / 2)] : null;
}

if (md) {
  const f = (x) => (Number.isFinite(x) ? String(Math.round(x)) : 'n/a');
  const cell = (s) => (s && s.n >= 2 ? `${f(s.p50)} [${f(s.p50ci[0])}, ${f(s.p50ci[1])}] / ${f(s.p95)} [${f(s.p95ci[0])}, ${f(s.p95ci[1])}]` : 'n/a');
  for (const tagSet of [...new Set(out.groups.map((g) => g.tagSet))]) {
    for (const kind of ['chunks', 'meta', 'plain']) {
      const sub = out.groups.filter((g) => g.tagSet === tagSet && g.kind === kind).sort((a, b) => REGION_ORDER.indexOf(a.region) - REGION_ORDER.indexOf(b.region));
      if (!sub.length) continue;
      console.log(`\n### ${tagSet} probes, ${kind}\n`);
      console.log('| Region | Edge state (cf-cache-status) | N | first byte p50 [CI] / p95 [CI] ms | total p50 [CI] / p95 [CI] ms | tcp p50 | tls p50 | PoPs |');
      console.log('|---|---|--:|---|---|--:|--:|---|');
      for (const g of sub) {
        for (const cls of ['HIT', 'non-HIT']) {
          const c = g.classes[cls];
          if (!c || c.n === 0) continue;
          const mix = cls === 'HIT' ? 'HIT' : Object.entries(g.cfMix).filter(([k]) => k !== 'HIT').map(([k, v]) => `${k} ${v}`).join(', ');
          console.log(`| ${g.region} | ${cls === 'HIT' ? 'HIT' : `not HIT (${mix})`} | ${c.n} | ${cell(c.metrics.firstByte)} | ${cell(c.metrics.total)} | ${f(c.metrics.tcp.p50)} | ${f(c.metrics.tls.p50)} | ${Object.entries(g.colos).map(([k, v]) => `${k} ${v}`).join(', ')} |`);
        }
      }
    }
  }
}
console.log(`wrote ${path.join(OUT_DIR, 'globalping_summary.json')}: ${ok.length} usable of ${rows.length} samples`);
