// Publish the viewer and its shared modules alongside the documentation.
import { cp, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const dist = resolve(repo, 'site/docs/dist');
await mkdir(dist, { recursive: true });
for (const directory of ['demo', 'chronozarr', 'shared', 'maplibre', 'geolibre', 'vendor', 'examples']) {
  await cp(resolve(repo, 'js', directory), resolve(dist, directory), {
    recursive: true,
    filter: source => !source.includes('/maplibre/verify'),
  });
}
await cp(resolve(repo, 'js/favicon.svg'), resolve(dist, 'demo/favicon.svg'));
// The documentation build already copies docs/public/_headers for all assets.
console.log('Copied demo and shared JavaScript assets.');
