#!/usr/bin/env node
// Copy installed assets to a new static directory. No bundler or network access is needed.
import { cp, mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const packageRoot = fileURLToPath(new URL('../', import.meta.url));
const [outputArg, ...args] = process.argv.slice(2);
if (!outputArg || outputArg === '--help' || (args.length && (args.length !== 2 || args[0] !== '--store'))) {
  console.error('Usage: chronozarr-viewer OUTPUT [--store URL]');
  process.exitCode = outputArg === '--help' ? 0 : 1;
} else {
  const output = path.resolve(outputArg);
  const store = args[1] ?? null;
  // Reject an existing directory, including symlinks, before writing any assets.
  await mkdir(output);
  for (const name of ['demo', 'shared', 'chronozarr', 'maplibre', 'vendor']) {
    await cp(path.join(packageRoot, name), path.join(output, name), { recursive: true });
  }
  await cp(path.join(packageRoot, 'favicon.svg'), path.join(output, 'favicon.svg'));
  const htmlPath = path.join(output, 'demo/index.html');
  const html = await readFile(htmlPath, 'utf8');
  await writeFile(htmlPath, html.replace('href="/favicon.svg"', 'href="../favicon.svg"'));
  // A private self-hosted viewer must never fall back to the public reference catalog.
  await writeFile(path.join(output, 'demo/catalog.json'), '[]\n');
  await writeFile(path.join(output, 'index.html'), `<!doctype html><meta charset="utf-8"><title>chronozarr viewer</title>
<script>
const viewer = new URL('demo/index.html', location.href);
const store = ${JSON.stringify(store).replaceAll('<', '\\u003c')};
if (store) viewer.searchParams.set('store', new URL(store, location.href).href);
location.replace(viewer.href);
</script>\n`);
  console.log(`Viewer copied to ${output}`);
}
