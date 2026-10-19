// Collect cold and warm edge samples of the public store from Globalping probes in four regions.
//
//   node scripts/fieldperf/gp_collect.mjs <kind> [--tags datacenter-network] [--target 30] [--max 48] [--regions frankfurt,sao_paulo,...] [--seed 20261016] [--tag label]
//
//   kind  chunks   random level-1 chunks of the default store (the batch the viewer fetches for its first frame)
//         meta     the root zarr.json behind a unique ?cb= query (every request is a miss at first, then the same key again)
//         plain    the root zarr.json as the viewer asks for it (no query), one request per turn, spread over time
//
// A "pair" is one probe asked for an object (intended cold), then the very same probe asked for the same object again
// (intended warm). Intent is only a plan: every sample is labelled by the cf-cache-status the response carried.
// Each region runs its own loop, one request at a time; the regions run side by side. Samples are appended to
// OUT_DIR/globalping_<kind>_<tag>.jsonl, so an interrupted run resumes by counting what is there.

import path from 'node:path';
import fs from 'node:fs';
import { DATA_ROOT, OUT_DIR, appendJsonl, readJsonl, rng, sleep, STORE } from './lib.mjs';
import { createHttp, distill, rate, waitFinished } from './gp.mjs';

const args = process.argv.slice(2);
const kind = args[0];
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const tags = [opt('tags', 'datacenter-network')];
const target = Number(opt('target', 30));
const maxPairs = Number(opt('max', 48));
const seed = Number(opt('seed', 20261016));
const label = opt('tag', tags[0].startsWith('eyeball') ? 'eyeball' : 'dc');
const REGIONS = {
  frankfurt: { country: 'DE', city: 'Frankfurt' },
  sao_paulo: { country: 'BR', city: 'Sao Paulo' },
  singapore: { country: 'SG', city: 'Singapore' },
  ashburn: { country: 'US', city: 'Ashburn' },
  los_angeles: { country: 'US', city: 'Los Angeles' },
};
const regionNames = opt('regions', 'frankfurt,sao_paulo,singapore,ashburn').split(',');
const HOST = 'data.chronozarr.org';
const PREFIX = new URL(STORE).pathname; // /ucayali_santa_maria_v03
const FILE = path.join(OUT_DIR, `globalping_${kind}_${label}.jsonl`);

if (!['chunks', 'meta', 'plain'].includes(kind)) {
  console.error('usage: gp_collect.mjs chunks|meta|plain [options]');
  process.exit(2);
}

// Level 1 of the default store: 117 timesteps, a 3 x 3 grid of cells (see cold_open_bare1.json). The viewer's
// first frame at 1440 x 900 x DPR 2 reads exactly these nine cells of one timestep.
const N_TIME = 117;
const GRID = 3;
const allObjects = [];
for (let t = 0; t < N_TIME; t++) for (let r = 0; r < GRID; r++) for (let c = 0; c < GRID; c++) allObjects.push(`${PREFIX}/1/data/c/${t}/0/${r}/${c}`);

/** A seeded shuffle, so each region draws its own objects (no object is sampled from two regions: a shared upper cache tier could warm it). */
function shuffled(items, rand) {
  const a = [...items];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}
const objectOrder = shuffled(allObjects, rng(seed));

// An object already requested in a region (any date, any probe set) is probably in that PoP's cache: skip it for "cold" draws.
const sampledBefore = new Map();
for (const dir of fs.readdirSync(DATA_ROOT)) {
  const full = path.join(DATA_ROOT, dir);
  if (!fs.statSync(full).isDirectory()) continue;
  for (const file of fs.readdirSync(full).filter((x) => /^globalping_chunks_.*\.jsonl$/.test(x))) {
    for (const r of readJsonl(path.join(full, file))) {
      if (!sampledBefore.has(r.region)) sampledBefore.set(r.region, new Set());
      sampledBefore.get(r.region).add(r.path);
    }
  }
}

const isHit = (s) => s.cfCacheStatus === 'HIT';
const existing = readJsonl(FILE);
const counts = (region) => {
  const rows = existing.filter((s) => s.region === region && s.statusCode === 200);
  return { hit: rows.filter(isHit).length, nonHit: rows.filter((s) => !isHit(s)).length, pairs: new Set(existing.filter((s) => s.region === region).map((s) => s.pairId)).size };
};

async function sample({ region, pairId, intent, objectPath, query, locations }) {
  const startedAt = Date.now();
  const created = await createHttp({ target: HOST, path: objectPath, query, locations, limit: 1, headers: {} });
  const finished = await waitFinished(created.id);
  const d = distill(finished, 0);
  const row = { kind, tagSet: label, region, pairId, intent, path: objectPath, query, startedAt, localTime: new Date(startedAt).toISOString(), ...d, probeCount: created.probesCount };
  appendJsonl(FILE, row);
  existing.push(row);
  return { row, measurementId: created.id };
}

async function regionLoop(region) {
  const where = [{ ...REGIONS[region], tags }];
  const rand = rng(seed + regionNames.indexOf(region) * 7919);
  const mine = objectOrder.filter((p, i) => i % regionNames.length === regionNames.indexOf(region) && !sampledBefore.get(region)?.has(p));
  let pairIndex = counts(region).pairs;
  let failures = 0;
  while (true) {
    if (failures >= 25) {
      console.error(`[${region}] 25 failures in a row, stopping this region`);
      break;
    }
    const c = counts(region);
    if (c.pairs >= maxPairs) break;
    if (kind !== 'plain' && c.nonHit >= target && c.hit >= target) break;
    if (kind === 'plain' && c.pairs >= target) break;
    const pairId = `${region}-${pairIndex}`;
    try {
      if (kind === 'plain') {
        await sample({ region, pairId, intent: 'natural', objectPath: `${PREFIX}/zarr.json`, query: '', locations: where });
        await sleep(20000 + rand() * 10000); // spread over time: the object's own 5-minute freshness decides the label
      } else {
        const objectPath = kind === 'chunks' ? mine[pairIndex % mine.length] : `${PREFIX}/zarr.json`;
        const query = kind === 'meta' ? `cb=fp${Date.now().toString(36)}${Math.floor(rand() * 1e9).toString(36)}` : '';
        const cold = await sample({ region, pairId, intent: 'cold', objectPath, query, locations: where });
        if (cold.row.status === 'finished' && cold.row.statusCode === 200) {
          await sleep(1500);
          await sample({ region, pairId, intent: 'warm', objectPath, query, locations: cold.measurementId });
        }
      }
    } catch (error) {
      failures += 1;
      console.error(`[${region}] pair ${pairId}: ${error.message.slice(0, 300)}`);
      if (/-> (400|422):/.test(error.message)) {
        console.error(`[${region}] the API refused the request itself; not retrying`);
        break;
      }
      await sleep(Math.min(300000, 5000 * 2 ** Math.min(failures, 6)));
      continue;
    }
    failures = 0;
    pairIndex += 1;
    const cc = counts(region);
    console.log(`[${region}] pairs ${cc.pairs}  HIT ${cc.hit}  non-HIT ${cc.nonHit}   budget left ${rate.remaining}`);
    await sleep(500 + rand() * 1500);
  }
  console.log(`[${region}] done: ${JSON.stringify(counts(region))}`);
}

console.log(`kind=${kind} tags=${tags} target=${target} max=${maxPairs} regions=${regionNames} file=${FILE}`);
await Promise.all(regionNames.map((region) => regionLoop(region)));
console.log(`finished; tests consumed this run: ${rate.consumedTotal}; 429s: ${rate.requests429}`);
