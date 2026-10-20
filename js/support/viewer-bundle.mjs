#!/usr/bin/env node
// Copy the built viewer to a new static directory. The package carries the viewer as `npm pack` built it (dist/viewer, see
// build-viewer.mjs); a checkout builds it now, which needs esbuild (`npm ci` in js/). The host needs neither.
import { cp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
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
  try {
    if (existsSync(path.join(packageRoot, 'demo/viewer.js'))) {
      const { buildViewer } = await import('./build-viewer.mjs');
      await buildViewer(output);
    } else {
      await cp(path.join(packageRoot, 'dist/viewer/demo'), path.join(output, 'demo'), { recursive: true });
    }
  } catch (error) {
    await rm(output, { recursive: true, force: true });
    throw error.code === 'ERR_MODULE_NOT_FOUND' && /esbuild/.test(error.message)
      ? new Error(`building the viewer from a checkout needs esbuild: run \`npm ci\` in ${packageRoot} first (${error.message})`)
      : error;
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
