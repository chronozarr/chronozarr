// The processes around the browser: two range-capable static servers (the repository's pages and scripts; the data:
// store and COGs) and TiTiler in a container.
//
// The data server is the one origin for systems A and B (the Zarr store) and for system C (TiTiler reads the COGs from
// it over HTTP range requests, as it would read them from object storage). The pages come from a second origin, so
// every data request is cross-origin as it is from a CDN.

import { execFile } from 'node:child_process';
import { createReadStream } from 'node:fs';
import { readdir } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';
import { startStaticServer } from '../../js/support/static-server.js';
import { DATA_ROOT, REPO_ROOT, TITILER, TITILER_TILE_QUERY } from './config.mjs';
import { tileOf } from './view.mjs';

const run = promisify(execFile);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export const startAppServer = () => startStaticServer(REPO_ROOT);
export const startDataServer = () => startStaticServer(DATA_ROOT);

/** The image reference: the pinned digest, so a moved tag cannot change what runs. */
export const titilerImageRef = () => `${TITILER.image}@${TITILER.digest}`;

/** Fail early, with the command to run, when Docker or the pinned image is missing; pull the image by digest when absent. */
export async function ensureTitilerImage() {
  try {
    await run('docker', ['version', '--format', '{{.Server.Version}}']);
  } catch (error) {
    throw new Error(`Docker is not running (${String(error.stderr ?? error.message).trim().split('\n')[0]}). Start Docker Desktop and rerun.`);
  }
  try {
    await run('docker', ['image', 'inspect', titilerImageRef()]);
  } catch {
    console.log(`pulling ${titilerImageRef()}`);
    await run('docker', ['pull', titilerImageRef()], { maxBuffer: 64 * 1024 * 1024 });
  }
}

/** What the benchmark records about the image: tag, digest, creation date and the library versions TiTiler reports at /healthz. */
export async function titilerImageInfo() {
  const { stdout } = await run('docker', ['image', 'inspect', titilerImageRef(), '--format', '{{.Id}}|{{.Created}}|{{.Size}}|{{.Architecture}}|{{.Os}}']);
  const [id, created, size, architecture, os] = stdout.trim().split('|');
  return { image: TITILER.image, tag: TITILER.tag, digest: TITILER.digest, imageId: id, created, sizeBytes: Number(size), architecture, os };
}

/**
 * Start a TiTiler container (published on a free port of 127.0.0.1), wait until it answers, and warm every gunicorn
 * worker: Python, GDAL and the first COG open are paid before the session, not by its first tile. The warm-up reads COGs of
 * dates the sessions never show, in waves of concurrent tile requests so that the workers share them.
 * `cogUrl(t)` is the URL, as the container sees it, of the COG of timestep t.
 */
export async function startTitiler({ cogUrl, workers = TITILER.workers }) {
  const name = `chronozarr-bench-titiler-${process.pid}-${Date.now()}`;
  const env = Object.entries({ ...TITILER.env, WEB_CONCURRENCY: String(workers) }).flatMap(([key, value]) => ['-e', `${key}=${value}`]);
  await run('docker', ['run', '-d', '--rm', '--name', name, '-p', '127.0.0.1::80', ...env, titilerImageRef()]);
  const stop = async () => {
    await run('docker', ['rm', '-f', name]).catch(() => {});
  };
  try {
    const published = (await run('docker', ['port', name, '80/tcp'])).stdout.trim().split('\n')[0];
    const url = `http://127.0.0.1:${published.split(':').at(-1)}`;
    let health = null;
    for (let attempt = 0; attempt < 120 && health === null; attempt++) {
      try {
        const response = await fetch(`${url}/healthz`);
        if (response.ok) health = await response.json();
      } catch {
        // not listening yet
      }
      if (health === null) await sleep(500);
    }
    if (health === null) throw new Error(`TiTiler did not answer on ${url}/healthz within 60 s: ${(await run('docker', ['logs', '--tail', '20', name]).catch((e) => ({ stdout: e.message }))).stdout}`);
    const centre = tileOf(-75.005, -7.635, 12);
    for (let wave = 0; wave < 3; wave++) {
      const requests = Array.from({ length: workers * 4 }, async (_, i) => {
        const date = TITILER.warmupDates[(wave * workers * 4 + i) % TITILER.warmupDates.length];
        const tile = await fetch(`${url}/cog/tiles/WebMercatorQuad/${centre.z}/${centre.x + (i % 2)}/${centre.y + (i % 3) - 1}.png?url=${encodeURIComponent(cogUrl(date))}&${TITILER_TILE_QUERY}`);
        if (!tile.ok) throw new Error(`TiTiler warm-up tile failed: HTTP ${tile.status} ${(await tile.text()).slice(0, 300)}`);
        await tile.arrayBuffer();
      });
      await Promise.all(requests);
    }
    return { url, name, health, workers, stop };
  } catch (error) {
    await stop();
    throw error;
  }
}

/**
 * Read every file under `dirs` once, so that the operating system's file cache holds the store and the COGs when a round
 * starts and no system is the first to pay for reading a file from disk. Returns {files, bytes, ms}.
 */
export async function warmFileCache(dirs) {
  const started = performance.now();
  const files = [];
  const walk = async (dir) => {
    for (const entry of await readdir(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) await walk(full);
      else if (entry.isFile()) files.push(full);
    }
  };
  for (const dir of dirs) await walk(dir);
  let bytes = 0;
  let next = 0;
  const worker = async () => {
    while (next < files.length) {
      const file = files[next++];
      for await (const chunk of createReadStream(file)) bytes += chunk.length;
    }
  };
  await Promise.all(Array.from({ length: 16 }, worker));
  return { files: files.length, bytes, ms: Math.round(performance.now() - started) };
}
