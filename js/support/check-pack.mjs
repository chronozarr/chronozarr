// prepack guard: the tarball must hold exactly what the package's entry points load, plus the files npm
// requires and the vendored licenses. Runs on `npm pack` and `npm publish` from js/ (package.json "prepack").
//
// The package ships source files unchanged and has no build step, so a file missing from "files" is a
// runtime 404 for a consumer, and unrelated tests or fixtures are noise in every install.
// The complete viewer is an intentional package asset. This script follows HTML script/image/link assets,
// relative imports, dynamic imports and
// `new URL('./x.js', import.meta.url)` references from every `exports` target (the decode worker is one of
// them) and compares that set with the file list of `npm pack --dry-run`.
//
//   node support/check-pack.mjs        exit 1 with the list of problems, or print a one-line summary

import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';

const JS_DIR = path.resolve(import.meta.dirname, '..');
const ROOT_LICENSE = path.join(JS_DIR, '..', 'LICENSE');
const posix = (p) => p.split(path.sep).join('/');
const RELATIVE = /(?:\bfrom\s*|\bimport\s*\(?\s*|new URL\(\s*)(['"])(\.{1,2}\/[^'"]+)\1/g;
const HTML_ASSET = /<(?:script|link|img)\b[^>]*\b(?:src|href)\s*=\s*(['"])([^'"]+)\1/gi;
const VENDORED = ['vendor/numcodecs/', 'vendor/zarrita/', 'vendor/zarrita-storage/'];

const pkg = JSON.parse(readFileSync(path.join(JS_DIR, 'package.json'), 'utf8'));
const problems = [];

function leaves(value) {
  if (typeof value === 'string') return [value];
  return Object.values(value ?? {}).flatMap(leaves);
}

function isCommentLine(line) {
  const trimmed = line.trimStart();
  return trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*');
}

/** Files (relative to js/, posix) reachable from `entries` through relative references. */
function reachable(entries) {
  const seen = new Set();
  const visit = (file, from) => {
    if (seen.has(file)) return;
    let source;
    try {
      source = readFileSync(path.join(JS_DIR, file), 'utf8');
    } catch {
      problems.push(`${file} is referenced from ${from} but does not exist`);
      return;
    }
    seen.add(file);
    if (file.endsWith('.html')) {
      for (const match of source.matchAll(HTML_ASSET)) {
        const asset = match[2].split(/[?#]/)[0];
        if (!asset || /^[a-z][a-z\d+.-]*:/i.test(asset) || asset.startsWith('//')) continue;
        const target = asset.startsWith('/') ? asset.slice(1) : path.join(path.dirname(file), asset);
        visit(posix(path.normalize(target)), file);
      }
      return;
    }
    if (!/\.m?js$/.test(file)) return;
    const code = source.split('\n').filter((line) => !isCommentLine(line)).join('\n');
    for (const match of code.matchAll(RELATIVE)) {
      visit(posix(path.normalize(path.join(path.dirname(file), match[2]))), file);
    }
  };
  for (const entry of entries) visit(posix(path.normalize(entry)), 'package.json exports');
  return seen;
}

function packedFiles() {
  const output = execFileSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], { cwd: JS_DIR, encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] });
  return new Set(JSON.parse(output)[0].files.map((file) => file.path));
}

if (pkg.private === true) problems.push('package.json has "private": true, so npm will refuse to publish it');
if (!/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(pkg.version ?? '')) problems.push(`package.json version "${pkg.version}" is not a semver version`);

const needed = reachable([...leaves(pkg.exports), ...leaves(pkg.bin), 'demo/index.html']);
const packed = packedFiles();

for (const file of [...needed].sort()) {
  if (!packed.has(file)) problems.push(`${file} is loaded by the entry points but is not in the tarball; add it to "files" in package.json`);
}
const allowed = (file) => needed.has(file) || ['package.json', 'README.md', 'LICENSE', 'favicon.svg', 'demo/index.html', 'demo/catalog.json'].includes(file) || VENDORED.some((prefix) => file.startsWith(prefix));
for (const file of [...packed].sort()) {
  if (!allowed(file)) problems.push(`${file} is in the tarball but nothing the entry points load reaches it; remove it from "files" or add an export that needs it`);
}
for (const file of ['README.md', 'LICENSE', 'demo/index.html', 'demo/catalog.json', 'favicon.svg', ...VENDORED.map((prefix) => `${prefix}LICENSE`)]) {
  if (!packed.has(file)) problems.push(`${file} is missing from the tarball`);
}
try {
  if (!readFileSync(path.join(JS_DIR, 'LICENSE')).equals(readFileSync(ROOT_LICENSE))) problems.push('js/LICENSE differs from the repository LICENSE; copy it again (cp ../LICENSE LICENSE)');
} catch (error) {
  problems.push(`cannot compare js/LICENSE with ${ROOT_LICENSE}: ${error.message}`);
}

if (problems.length > 0) {
  console.error(`check-pack: ${problems.length} problem${problems.length === 1 ? '' : 's'}`);
  for (const problem of problems) console.error(`  - ${problem}`);
  process.exit(1);
}
console.error(`check-pack: ok (${packed.size} files, ${needed.size} reachable from exports)`);
