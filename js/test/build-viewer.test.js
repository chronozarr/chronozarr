// The built viewer (support/build-viewer.mjs) is what the site and the package publish, so the property that justifies it
// is tested on its files: a browser reads the viewer, the decode worker and the benchmarks each as one file, because none
// of them imports another file before it runs. Whatever they load later is loaded by `import()` when it is needed.
// (e2e/viewer-built.spec.js measures the same thing in a browser, from the network log.)

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { buildViewer } from '../support/build-viewer.mjs';

let root;
let demo;
before(async () => {
  root = await mkdtemp(path.join(tmpdir(), 'chronozarr-build-test-'));
  demo = await buildViewer(root);
});
after(() => rm(root, { recursive: true, force: true }));

const files = (dir = demo) => readdirSync(dir, { withFileTypes: true }).flatMap((entry) => (entry.isDirectory() ? files(path.join(dir, entry.name)) : [path.join(dir, entry.name)]));
const scripts = () => files().filter((file) => file.endsWith('.js'));
const text = (file) => readFileSync(path.join(demo, file), 'utf8');

/** `import ... from "./x"`, `import "./x"` and `export ... from "./x"` in minified output; an `import(...)` call is not one. */
const STATIC_IMPORTS = /(?:^|[;{}\n])\s*(?:import|export)\s*(?:[\w${},*\s]+?\s*from\s*)?["'](\.{1,2}\/[^"']+)["']/g;
/** Files that code names by a relative path: `import("./x")` and `new URL("./x", import.meta.url)`. */
const NAMED_FILES = /(?:import\(\s*|new URL\(\s*)["'](\.{1,2}\/[^"']+)["']/g;

test('the viewer, the decode worker and the benchmarks import nothing before they run', () => {
  for (const file of ['viewer.js', 'decode-worker.js', 'bench.js', 'codecs/zstd.js', 'codecs/blosc.js', 'codecs/lz4.js']) {
    assert.deepEqual([...text(file).matchAll(STATIC_IMPORTS)].map((m) => m[1]), [], `${file} has a static import: one more round trip in front of it`);
  }
});

test('no script has a static import, so the only sequential scripts are the lazy ones', () => {
  for (const file of scripts()) assert.deepEqual([...readFileSync(file, 'utf8').matchAll(STATIC_IMPORTS)].map((m) => m[1]), [], path.relative(demo, file));
});

test('every file that the code names exists next to it, and nothing points back into the source tree', () => {
  for (const file of scripts()) {
    const code = readFileSync(file, 'utf8');
    for (const [, named] of code.matchAll(NAMED_FILES)) {
      if (named.endsWith('catalog.json')) continue;
      assert.ok(existsSync(path.join(path.dirname(file), named)), `${path.relative(demo, file)} names ${named}, which the build did not write`);
    }
    assert.doesNotMatch(code, /["'`]\.\.\/(vendor|chronozarr|shared|maplibre|demo)\//, `${path.relative(demo, file)} names a file of the source tree`);
  }
});

test('the viewer loads its decode worker, and the worker has zarrita inside it instead of a URL to import', () => {
  assert.match(text('viewer.js'), /new URL\(["']\.\/decode-worker\.js["'],import\.meta\.url\)/);
  const worker = text('decode-worker.js');
  assert.doesNotMatch(worker, /zarritaUrl/, 'the worker imports the registry it was built with, not the URL the pool sends');
  assert.match(worker, /zstd/, 'and the registry knows the codecs');
});

test('the codecs, the part of a store that varies, are loaded by the one copy in codecs/ from the viewer, the worker and the benchmarks', () => {
  for (const file of ['viewer.js', 'decode-worker.js', 'bench.js']) {
    for (const codec of ['zstd', 'blosc', 'lz4']) assert.ok(text(file).includes(`./codecs/${codec}.js`), `${file} loads codecs/${codec}.js`);
  }
});

test('the page is the one of js/demo, and it asks for the viewer script and for nothing else of the build', () => {
  const html = text('index.html');
  assert.equal(html, readFileSync(new URL('../demo/index.html', import.meta.url), 'utf8'));
  assert.deepEqual([...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map((m) => m[1]), ['viewer.js']);
  assert.ok(existsSync(path.join(demo, 'catalog.json')));
});

test('the licenses of what the bundles contain go with them: the vendored packages in LICENSES.txt, gifenc in the viewer script', () => {
  const licenses = text('LICENSES.txt');
  for (const name of ['zarrita', '@zarrita/storage', 'numcodecs', 'proj4 and wkt-parser']) assert.ok(licenses.includes(`${name}\n`), name);
  assert.equal(licenses.match(/Permission is hereby granted/g).length, 5, 'one license text per package: zarrita, @zarrita/storage, numcodecs, proj4 and wkt-parser');
  assert.match(text('viewer.js'), /gifenc 1\.0\.3[\s\S]{0,200}Matt DesLauriers[\s\S]*Permission is hereby granted/, 'the notice of the one package that has none beside it');
});

test('proj4 is a chunk of its own that only a store with a CRS of its own loads', () => {
  const chunks = readdirSync(path.join(demo, 'chunks'));
  assert.ok(chunks.some((name) => /^proj4-/.test(name)), 'a proj4 chunk');
  assert.doesNotMatch(text('viewer.js'), /Fast_Transverse_Mercator|Extended_Transverse_Mercator/, 'proj4 implementation stays in its lazy chunk; CRS comparison may name builtin definitions');
});
