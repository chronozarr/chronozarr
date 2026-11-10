// The long time axis of the gate store (support/gate-bench/store.mjs --times): the metadata of a store of more timesteps than
// were generated, and the chunk files that are hard links to those that were.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { COMPRESSORS, compressedStore } from '../e2e/make-node-fixtures.mjs';
import { extendTimeAxis, writeLinkedTree } from '../support/gate-bench/store.mjs';

// 2 x 2 chunks at level 0 and one at level 1, 3 timesteps, one band: a few dozen small objects.
const SMALL = { nTime: 3, nBand: 1, bands: ['B04'], height: 600, width: 600, chunk: 512, nLevels: 2, sharded: false, consolidated: true, nodata: 0, transform: [10, 0, 500000, 0, -10, 4000000] };
const small = () => compressedStore(SMALL, COMPRESSORS.zstd);
const json = (files, key) => JSON.parse(new TextDecoder().decode(files.get(key)));

test('every array of a longer store has the longer time axis, in its own file and in the consolidated metadata', () => {
  const { files } = extendTimeAxis(small(), 3, 8);
  for (const key of ['/0/data/zarr.json', '/1/data/zarr.json']) assert.equal(json(files, key).shape[0], 8, key);
  const root = json(files, '/zarr.json');
  for (const [name, meta] of Object.entries(root.consolidated_metadata.metadata)) {
    if (meta.node_type === 'array') assert.equal(meta.shape[0], 8, name);
  }
  assert.equal(root.attributes.chronozarr.times.length, 8);
});

test('the timestamps continue at the step of the first two', () => {
  const { times } = json(extendTimeAxis(small(), 3, 6).files, '/zarr.json').attributes.chronozarr;
  assert.deepEqual(times, ['2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z', '2024-01-03T00:00:00Z', '2024-01-04T00:00:00Z', '2024-01-05T00:00:00Z', '2024-01-06T00:00:00Z']);
});

test('the chunk of a new timestep is a link to the chunk of the timestep at the same position in the first ones', () => {
  const { files, links } = extendTimeAxis(small(), 3, 8);
  const byPath = new Map(links.map((link) => [link.path, link.target]));
  assert.equal(byPath.get('/0/data/c/3/0/0/0'), '/0/data/c/0/0/0/0');
  assert.equal(byPath.get('/0/data/c/4/0/1/1'), '/0/data/c/1/0/1/1');
  assert.equal(byPath.get('/0/data/c/5/0/0/1'), '/0/data/c/2/0/0/1');
  assert.equal(byPath.get('/0/data/c/6/0/0/0'), '/0/data/c/0/0/0/0');
  assert.equal(byPath.get('/0/data/c/7/0/0/0'), '/0/data/c/1/0/0/0');
  assert.equal(byPath.get('/1/data/c/7/0/0/0'), '/1/data/c/1/0/0/0');
  // 5 new timesteps of 5 chunks (4 at level 0, 1 at level 1), and every target is a file of the store.
  assert.equal(links.length, 25);
  for (const { path: linkPath, target } of links) {
    assert.ok(files.has(target), target);
    assert.ok(!files.has(linkPath), `${linkPath} is a link, not a file`);
  }
});

test('the same number of timesteps changes nothing, and fewer than were generated is an error', () => {
  const files = small();
  const same = extendTimeAxis(files, 3, 3);
  assert.equal(same.files, files);
  assert.deepEqual(same.links, []);
  assert.throws(() => extendTimeAxis(files, 3, 2), /--times must be an integer of at least 3/);
  assert.throws(() => extendTimeAxis(files, 3, 4.5), /--times must be an integer of at least 3/);
});

test('the written store has a file for every timestep, the new ones sharing the bytes and the disk of the first', async () => {
  const dir = path.join(await mkdtemp(path.join(os.tmpdir(), 'gate-store-test-')), 'store');
  await writeLinkedTree(dir, extendTimeAxis(small(), 3, 7));
  const first = path.join(dir, '0/data/c/1/0/1/0');
  const fourth = path.join(dir, '0/data/c/4/0/1/0');
  const last = path.join(dir, '0/data/c/6/0/1/0');
  assert.deepEqual(await readFile(fourth), await readFile(first));
  assert.equal((await stat(fourth)).ino, (await stat(first)).ino, 'one file on disk');
  assert.equal((await stat(first)).nlink, 2);
  assert.notDeepEqual(await readFile(last), await readFile(first), 'timestep 6 is timestep 0, not 1');
  assert.equal(JSON.parse(await readFile(path.join(dir, '0/data/zarr.json'), 'utf8')).shape[0], 7);
});
