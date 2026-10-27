// Check the existing DOM-free browser reader against the imported source values.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { openStore } from '../../js/chronozarr/decoder.js';
import { startStaticServer } from '../../js/support/static-server.js';

const directory = path.resolve(process.argv[2]);
const receipt = JSON.parse(await readFile(path.join(directory, 'receipt.json'), 'utf8'));
const server = await startStaticServer(directory);
const checks = [];
try {
  for (const source of receipt.sources) {
    const store = await openStore(`${server.url}/${source.output}/`, { workers: 0 });
    try {
      const times = [];
      for (let t = 0; t < source.expected_float32.length; t++) {
        const cell = await store.getCell(0, 0, 0, t);
        const rows = [];
        for (let y = 0; y < 2; y++) rows.push(Array.from(cell.data.slice(y * cell.chunkWidth, y * cell.chunkWidth + 2)));
        assert.deepEqual(rows, source.expected_float32[t]);
        times.push(rows);
      }
      const requestsBefore = store.stats.network.requests;
      await store.getCell(0, 0, 0, 0);
      assert.equal(store.stats.network.requests, requestsBefore, 'warm timestep is cached');
      checks.push({ source: source.name, exactFloat32Values: true, warmReadRequests: 0, times });
    } finally { store.close(); }
  }
} finally { await server.close(); }
console.log(JSON.stringify({ reader: 'existing chronozarr DOM-free JavaScript reader over HTTP', checks }));
