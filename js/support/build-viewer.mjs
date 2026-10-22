// Builds the viewer that sites and packages publish: the page of js/demo with its modules bundled, so a browser needs one
// request for the viewer instead of one per module. The sources stay as they are, for development and the tests.
//
//   node support/build-viewer.mjs <outDir>      writes <outDir>/demo/ (needs `npm ci` in js/, for esbuild)
//
// Without a bundle the browser reads viewer.js, then the modules it imports, then theirs: 61 files in 5 to 9 sequential
// round trips before anything runs (docs/evidence.md, "Field performance"). The output of <outDir>/demo:
//
//   index.html, catalog.json    copied from js/demo
//   LICENSES.txt                the license texts of the vendored packages (the bundles keep only their header comments)
//   viewer.js                   the viewer and the reader, one file with no static imports
//   decode-worker.js            the decode worker with zarrita's codec registry inside it
//   bench.js                    `chronozarr.bench()` and the other console benchmarks (loaded only when one is called)
//   codecs/{zstd,blosc,lz4}.js  the WebAssembly codecs, one copy that the viewer and the worker both load, when a store needs it
//   chunks/*.js                 loaded only for a store whose CRS needs proj4
//
// Why each piece is its own file: the codecs are 750 KB, 600 KB and 40 KB and most stores need one of them; proj4 is
// 70 KB and few stores need it; bench.js is 36 KB that no visitor runs. A worker is a separate script by definition.
// bench.js and the worker are built on their own because a chunk that two entry points share would be a static import,
// one more round trip in front of everything that imports it.

import { build } from 'esbuild';
import { copyFile, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

const JS_DIR = path.resolve(import.meta.dirname, '..');

/** The vendored packages whose license text sits in their folder; a bundle keeps their header comments but not these files. */
const LICENSED = [
  ['zarrita', 'vendor/zarrita'],
  ['@zarrita/storage', 'vendor/zarrita-storage'],
  ['numcodecs', 'vendor/numcodecs'],
  ['proj4 and wkt-parser', 'vendor/proj4'],
];

/** The codecs stay out of the bundles: every bundle imports the one copy in codecs/, a path that is the same for each. */
const lazyCodecs = {
  name: 'lazy-codecs',
  setup(esbuild) {
    esbuild.onResolve({ filter: /numcodecs\/(zstd|blosc|lz4)\.js$/ }, (args) => {
      if (args.kind === 'entry-point') return undefined;
      return { path: `./codecs/${path.basename(args.path)}`, external: true };
    });
  },
};

/** bench.js is the one lazy import that shares modules with the viewer: it is built apart and left as `import('./bench.js')`. */
const benchApart = {
  name: 'bench-apart',
  setup(esbuild) {
    esbuild.onResolve({ filter: /^\.\/bench\.js$/ }, () => ({ path: './bench.js', external: true }));
  },
};

/**
 * The decode worker loads zarrita's codec registry from a URL that decoder.js sends it, because a worker sees no import
 * map and a package can be served from anywhere. In the build the registry is part of the worker, so the worker imports
 * it statically and the URL is not a file any more. Each edit must find its text: a change to the source that moves
 * one fails the build, not the page.
 */
const REGISTRY_EDITS = {
  'chronozarr/decode-worker.js': [
    { from: 'zarrita = await import(message.zarritaUrl);', to: 'zarrita = bundledZarrita;', head: "import * as bundledZarrita from '../vendor/zarrita/codecs.js';\n" },
  ],
  'chronozarr/decoder.js': [{ from: "new URL('../vendor/zarrita/codecs.js', import.meta.url).href", to: "'bundled'" }],
};
const registryInWorker = {
  name: 'registry-in-worker',
  setup(esbuild) {
    esbuild.onLoad({ filter: /chronozarr[\\/](decode-worker|decoder)\.js$/ }, async ({ path: file }) => {
      const edits = REGISTRY_EDITS[path.relative(JS_DIR, file).split(path.sep).join('/')];
      if (!edits) return undefined;
      let contents = await readFile(file, 'utf8');
      for (const { from, to, head = '' } of edits) {
        if (!contents.includes(from)) throw new Error(`${file} no longer contains \`${from}\`: update REGISTRY_EDITS in support/build-viewer.mjs`);
        contents = head + contents.replace(from, to);
      }
      return { contents, loader: 'js', resolveDir: path.dirname(file) };
    });
  },
};

const COMMON = { bundle: true, format: 'esm', target: 'es2022', minify: true, logLevel: 'warning' };

/** Write the viewer to `<outDir>/demo`. Replaces what is in that folder. */
export async function buildViewer(outDir) {
  const demo = path.join(outDir, 'demo');
  await rm(demo, { recursive: true, force: true });
  await mkdir(demo, { recursive: true });
  for (const file of ['index.html', 'catalog.json']) await copyFile(path.join(JS_DIR, 'demo', file), path.join(demo, file));
  const licenses = await Promise.all(
    LICENSED.map(async ([name, folder]) => `${name}\n${'='.repeat(name.length)}\n\n${(await readFile(path.join(JS_DIR, folder, 'LICENSE'), 'utf8')).trim()}\n`),
  );
  await writeFile(path.join(demo, 'LICENSES.txt'), `Licenses of the third-party code in this viewer. The notice of gifenc is the comment at the end of viewer.js.\n\n${licenses.join('\n')}`);

  const entry = (...parts) => path.join(JS_DIR, ...parts);
  await Promise.all([
    build({ ...COMMON, entryPoints: { viewer: entry('demo/viewer.js') }, outdir: demo, splitting: true, chunkNames: 'chunks/[name]-[hash]', plugins: [lazyCodecs, benchApart, registryInWorker] }),
    build({ ...COMMON, entryPoints: { bench: entry('demo/bench.js') }, outdir: demo, plugins: [lazyCodecs, registryInWorker] }),
    build({ ...COMMON, entryPoints: { 'decode-worker': entry('chronozarr/decode-worker.js') }, outdir: demo, plugins: [lazyCodecs, registryInWorker] }),
    build({
      ...COMMON,
      entryPoints: ['zstd', 'blosc', 'lz4'].map((name) => entry('vendor/numcodecs', `${name}.js`)),
      outdir: path.join(demo, 'codecs'),
      plugins: [lazyCodecs],
    }),
  ]);
  return demo;
}

if (import.meta.filename === process.argv[1]) {
  const [outDir] = process.argv.slice(2);
  if (!outDir) {
    console.error('Usage: node support/build-viewer.mjs <outDir>');
    process.exit(1);
  }
  // stderr, like check-pack: `npm pack --json` runs this as prepack and reads the JSON on stdout.
  console.error(`Viewer built in ${await buildViewer(path.resolve(outDir))}`);
}
