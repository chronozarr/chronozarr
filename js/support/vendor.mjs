// Copies zarrita and its codec dependencies from js/node_modules into js/vendor, so the viewer loads them
// from its own origin and needs no third-party host at runtime.
//
//   node js/support/vendor.mjs           regenerate js/vendor/{zarrita,zarrita-storage,numcodecs}
//   node js/support/vendor.mjs --check   exit 1 unless the vendored files are what regeneration would write
//
// The files are the published builds (package "files": dist/ of zarrita, @zarrita/storage and numcodecs),
// unbundled, reached by following imports from zarrita's entry point. Three things change, each recorded in
// the file's header comment: a header naming package, version, license and the SHA-256 of the original; bare
// import specifiers of another package ("numcodecs/zstd") rewritten to relative paths, because workers do not
// see import maps; and "//# sourceMappingURL" comments removed, because the maps are not shipped.

import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const JS_DIR = path.resolve(import.meta.dirname, '..');
const MODULES = path.join(JS_DIR, 'node_modules');
const VENDOR = path.join(JS_DIR, 'vendor');

const PACKAGES = {
  zarrita: { root: 'dist/src', out: 'zarrita', extra: '' },
  '@zarrita/storage': { root: 'dist/src', out: 'zarrita-storage', extra: '' },
  numcodecs: {
    root: 'dist',
    out: 'numcodecs',
    extra:
      ' blosc.js, lz4.js and zstd.js embed WebAssembly builds of Blosc (with its bundled zlib and snappy), LZ4 and Zstandard;' +
      ' those C libraries carry their own permissive upstream licenses (BSD-style, zlib) and numcodecs ships no separate notice for them.',
  },
};
const ENTRY = { pkg: 'zarrita', file: 'dist/src/index.js' };
const SPECIFIER = /(\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)(["'])([^"']+)\2/g;

const sha256 = (text) => createHash('sha256').update(text).digest('hex');
const posix = (p) => p.split(path.sep).join('/');

function packageInfo(name) {
  const dir = path.join(MODULES, name);
  const manifest = JSON.parse(readFileSync(path.join(dir, 'package.json'), 'utf8'));
  return { dir, manifest, version: manifest.version, license: manifest.license, repository: typeof manifest.repository === 'string' ? manifest.repository : manifest.repository?.url };
}

/** Which package file a bare specifier such as "numcodecs/zstd" or "@zarrita/storage/fetch" names, via the package's exports map. */
function resolveBare(specifier) {
  const parts = specifier.split('/');
  const name = specifier.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0];
  if (!PACKAGES[name]) throw new Error(`vendor: no rule for package "${name}" (imported as "${specifier}")`);
  const subpath = `.${specifier.slice(name.length)}`;
  const target = packageInfo(name).manifest.exports?.[subpath]?.import;
  if (!target) throw new Error(`vendor: "${specifier}" has no import target in ${name}'s exports map`);
  return { pkg: name, file: posix(path.normalize(target)) };
}

const outPathOf = ({ pkg, file }) => path.join(VENDOR, PACKAGES[pkg].out, path.relative(PACKAGES[pkg].root, file));

function inComment(text, index) {
  const lineStart = text.lastIndexOf('\n', index) + 1;
  const before = text.slice(lineStart, index).trimStart();
  return before.startsWith('*') || before.startsWith('//') || before.startsWith('/*');
}

/** Follow imports from the entry point; returns every file to vendor as {ref, original, text}. */
function collect() {
  const seen = new Map();
  const queue = [ENTRY];
  while (queue.length) {
    const ref = queue.pop();
    const key = `${ref.pkg}:${ref.file}`;
    if (seen.has(key)) continue;
    const original = readFileSync(path.join(packageInfo(ref.pkg).dir, ref.file), 'utf8');
    const imports = [];
    for (const match of original.matchAll(SPECIFIER)) {
      if (inComment(original, match.index)) continue;
      const specifier = match[3];
      const target = specifier.startsWith('.') ? { pkg: ref.pkg, file: posix(path.normalize(path.join(path.dirname(ref.file), specifier))) } : resolveBare(specifier);
      imports.push({ specifier, target, at: match.index + match[0].length - specifier.length - 1 });
      queue.push(target);
    }
    seen.set(key, { ref, original, imports });
  }
  return [...seen.values()];
}

function render({ ref, original, imports }) {
  const info = packageInfo(ref.pkg);
  const from = outPathOf(ref);
  let text = original;
  // Rewrite back to front so earlier offsets stay valid.
  for (const { specifier, target, at } of [...imports].reverse()) {
    if (specifier.startsWith('.') && ref.pkg === target.pkg) continue;
    let relative = posix(path.relative(path.dirname(from), outPathOf(target)));
    if (!relative.startsWith('.')) relative = `./${relative}`;
    text = text.slice(0, at) + relative + text.slice(at + specifier.length);
  }
  text = text.replace(/\n?\/\/# sourceMappingURL=\S+\s*$/, '\n');
  const header =
    `/*! Vendored by js/support/vendor.mjs; regenerate, do not edit.\n` +
    ` * package: ${ref.pkg} ${info.version}, license ${info.license}, ${info.repository ?? ''}\n` +
    ` * file:    ${ref.file}, sha256 of the published file ${sha256(original)}\n` +
    ` * changes: this header; bare imports of other packages rewritten to relative paths; sourceMappingURL comment removed.${PACKAGES[ref.pkg].extra}\n` +
    ` */\n`;
  return header + text;
}

/** Everything vendoring writes: path -> contents. */
export function buildVendorTree() {
  const files = new Map();
  for (const entry of collect()) files.set(outPathOf(entry.ref), render(entry));
  for (const [name, { out }] of Object.entries(PACKAGES)) {
    files.set(path.join(VENDOR, out, 'LICENSE'), readFileSync(path.join(packageInfo(name).dir, 'LICENSE'), 'utf8'));
  }
  return files;
}

function listFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? listFiles(path.join(dir, e.name)) : [path.join(dir, e.name)]));
}

/** Differences between what is on disk in the vendored directories and the regenerated tree. */
export function checkVendorTree() {
  const expected = buildVendorTree();
  const problems = [];
  for (const [file, text] of expected) {
    let actual = null;
    try {
      actual = readFileSync(file, 'utf8');
    } catch {
      problems.push(`missing ${path.relative(JS_DIR, file)}`);
      continue;
    }
    if (actual !== text) problems.push(`differs ${path.relative(JS_DIR, file)}`);
  }
  for (const { out } of Object.values(PACKAGES)) {
    const dir = path.join(VENDOR, out);
    if (!statSync(dir, { throwIfNoEntry: false })) continue;
    for (const file of listFiles(dir)) if (!expected.has(file)) problems.push(`unexpected ${path.relative(JS_DIR, file)}`);
  }
  return problems;
}

if (import.meta.filename === process.argv[1]) {
  if (process.argv.includes('--check')) {
    const problems = checkVendorTree();
    if (problems.length) {
      console.error(`vendored files are out of date (run node js/support/vendor.mjs):\n${problems.join('\n')}`);
      process.exit(1);
    }
    console.log('vendored files match js/node_modules');
  } else {
    for (const { out } of Object.values(PACKAGES)) rmSync(path.join(VENDOR, out), { recursive: true, force: true });
    const files = buildVendorTree();
    for (const [file, text] of files) {
      mkdirSync(path.dirname(file), { recursive: true });
      writeFileSync(file, text);
    }
    console.log(`vendored ${files.size} files into ${path.relative(process.cwd(), VENDOR)}`);
  }
}
