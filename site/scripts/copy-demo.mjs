// Publish the viewer and its shared modules alongside the documentation.
//
// /demo/ is the built viewer (js/support/build-viewer.mjs): one script instead of the 61 modules that a browser would read
// one level at a time. The reader, the MapLibre layer, the shared modules and the vendored libraries stay as the modules that
// pages import from this site.
import { cp, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const dist = resolve(repo, 'site/docs/dist');
await mkdir(dist, { recursive: true });

let buildViewer;
try {
  ({ buildViewer } = await import('../../js/support/build-viewer.mjs'));
} catch (error) {
  if (error.code !== 'ERR_MODULE_NOT_FOUND' || !/esbuild/.test(error.message)) throw error;
  throw new Error(`building the viewer needs esbuild: run \`npm ci\` in ${resolve(repo, 'js')} first (${error.message})`);
}
await buildViewer(dist);
for (const directory of ['chronozarr', 'shared', 'maplibre', 'geolibre', 'vendor', 'examples']) {
  await cp(resolve(repo, 'js', directory), resolve(dist, directory), {
    recursive: true,
    filter: source => !source.includes('/maplibre/verify'),
  });
}
await cp(resolve(repo, 'js/favicon.svg'), resolve(dist, 'demo/favicon.svg'));
// The documentation build already copies docs/public/_headers for all assets.
console.log('Built the viewer and copied the shared JavaScript assets.');
