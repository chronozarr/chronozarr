import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { checkVendorTree } from '../support/vendor.mjs';

const JS_DIR = path.resolve(import.meta.dirname, '..');
const listJs = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listJs(path.join(dir, e.name)) : e.name.endsWith('.js') ? [path.join(dir, e.name)] : []));
const VENDORED = ['zarrita', 'zarrita-storage', 'numcodecs'].flatMap((name) => listJs(path.join(JS_DIR, 'vendor', name)));
const skipNoModules = existsSync(path.join(JS_DIR, 'node_modules/zarrita')) ? false : 'js/node_modules is not installed';

test('vendored files are exactly what vendoring the pinned packages produces', { skip: skipNoModules }, () => {
  assert.deepEqual(checkVendorTree(), []);
});

test('every vendored file names its package, version, license and the hash of the published original', () => {
  assert.ok(VENDORED.length > 40);
  for (const file of VENDORED) {
    const head = readFileSync(file, 'utf8').slice(0, 900);
    assert.match(head, /^\/\*! Vendored by js\/support\/vendor\.mjs/, file);
    assert.match(head, /package: (zarrita|@zarrita\/storage|numcodecs) \d+\.\d+\.\d+, license MIT/, file);
    assert.match(head, /sha256 of the published file [0-9a-f]{64}/, file);
  }
  const versions = new Set(VENDORED.map((file) => readFileSync(file, 'utf8').match(/package: (\S+ \S+),/)[1]));
  assert.deepEqual([...versions].sort(), ['@zarrita/storage 0.2.0', 'numcodecs 0.3.2', 'zarrita 0.7.5']);
  for (const dir of ['zarrita', 'zarrita-storage', 'numcodecs']) assert.match(readFileSync(path.join(JS_DIR, 'vendor', dir, 'LICENSE'), 'utf8'), /^MIT License/);
});

test('no vendored file imports a bare specifier or a URL: workers and plain browsers resolve them all', () => {
  const specifier = /(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)(["'])([^"']+)\1/g;
  for (const file of VENDORED) {
    const text = readFileSync(file, 'utf8');
    for (const match of text.matchAll(specifier)) {
      const before = text.slice(text.lastIndexOf('\n', match.index) + 1, match.index).trimStart();
      if (before.startsWith('*') || before.startsWith('//')) continue;
      assert.match(match[2], /^\.\.?\//, `${path.relative(JS_DIR, file)} imports "${match[2]}"`);
    }
  }
});

test('the reader and its workers reach no third-party host: no CDN URL in the reader, support code or vendored loaders', () => {
  const files = [...listJs(path.join(JS_DIR, 'chronozarr')), ...listJs(path.join(JS_DIR, 'support')), ...VENDORED.filter((f) => !f.includes('/numcodecs/'))];
  for (const file of files) {
    const text = readFileSync(file, 'utf8');
    assert.doesNotMatch(text, /jsdelivr|unpkg\.com|cdnjs|skypack|esm\.sh|https?:\/\/cdn\./i, path.relative(JS_DIR, file));
  }
});

test('package.json pins zarrita, which only the vendoring script and the tests still need', () => {
  const manifest = JSON.parse(readFileSync(path.join(JS_DIR, 'package.json'), 'utf8'));
  assert.equal(manifest.dependencies, undefined, 'the reader has no runtime dependencies');
  assert.equal(manifest.devDependencies.zarrita, '0.7.5', 'an exact version, not a range');
});
