// Bundles page.js (@carbonplan/zarr-layer + proj4) into dist/page.js and copies maplibre-gl 6 next to it unbundled:
// its ES module starts its worker from a sibling file, which a bundle would break. page.html maps the bare
// specifier `maplibre-gl` to the copy.
import { copyFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { build } from 'esbuild';

const here = import.meta.dirname;
const maplibre = path.join(here, '../node_modules/maplibre-gl/dist');
await mkdir(path.join(here, 'dist/maplibre'), { recursive: true });
await build({
  entryPoints: [path.join(here, 'page.js')],
  bundle: true,
  format: 'esm',
  target: 'es2022',
  external: ['maplibre-gl'],
  outfile: path.join(here, 'dist/page.js'),
  logLevel: 'info',
});
for (const file of ['maplibre-gl.mjs', 'maplibre-gl-shared.mjs', 'maplibre-gl-worker.mjs']) {
  await copyFile(path.join(maplibre, file), path.join(here, 'dist/maplibre', file));
}
await copyFile(path.join(maplibre, 'maplibre-gl.css'), path.join(here, 'dist/maplibre-gl.css'));
