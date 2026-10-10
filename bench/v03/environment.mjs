// What a result needs to be interpreted later: machine, browser and GPU, library and image versions, the data, the
// settings of the run. Written next to the results as environment.json.

import { execFile } from 'node:child_process';
import { readFile, stat } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { COG_DIR, JUMP_DISTANCE, RAF_NOMINAL_MS, REPO_ROOT, START_T, STEPS, STORE_DIR, THINK_MS, TITILER, TITILER_TILE_QUERY, TRAILING_IDLE_MS, VIEWPORT } from './config.mjs';
import { titilerImageInfo } from './servers.mjs';
import { launchBrowser, newPage } from '../lib/harness.mjs';

const run = promisify(execFile);
const text = async (command, commandArgs, options = {}) => {
  try {
    return (await run(command, commandArgs, options)).stdout.trim();
  } catch (error) {
    return `unavailable: ${String(error.message).split('\n')[0]}`;
  }
};

const dirSize = async (dir, names) => {
  let bytes = 0;
  for (const name of names) bytes += (await stat(path.join(dir, name)).catch(() => ({ size: 0 }))).size;
  return bytes;
};

/** The browser's version and what WebGL2 reports for the GPU: a software renderer here would invalidate every number. */
async function browserInfo(servers) {
  const browser = await launchBrowser({ channel: 'chromium' });
  try {
    const { page } = await newPage(browser, { width: VIEWPORT.width, height: VIEWPORT.height, deviceScaleFactor: VIEWPORT.deviceScaleFactor });
    await page.goto(`${servers.app.url}/bench/v03/page-c.html`);
    const gpu = await page.evaluate(() => {
      const gl = document.createElement('canvas').getContext('webgl2');
      const ext = gl?.getExtension('WEBGL_debug_renderer_info');
      return { webgl2: Boolean(gl), renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null, vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null, userAgent: navigator.userAgent, hardwareConcurrency: navigator.hardwareConcurrency };
    });
    if (!gpu.webgl2 || /swiftshader|software|llvmpipe/i.test(gpu.renderer ?? '')) {
      throw new Error(`the browser has no hardware WebGL2 (renderer: ${gpu.renderer}); the benchmark needs the real GPU. Run it in a desktop session on this machine, not over SSH or in a container.`);
    }
    return { version: browser.version(), ...gpu };
  } finally {
    await browser.close();
  }
}

/** Versions of the Python side (store reader, COG writer): reference.py and `chronozarr export-cog` run in this environment. */
async function pythonInfo() {
  const code = 'import json, sys, zarr, rasterio; from importlib.metadata import version; from rasterio.env import GDALVersion; print(json.dumps({"python": sys.version.split()[0], "zarr": zarr.__version__, "rasterio": rasterio.__version__, "gdal": str(GDALVersion.runtime()), "chronozarr": version("chronozarr")}))';
  const out = await text('uv', ['run', '--extra', 'geo', 'python', '-c', code], { cwd: REPO_ROOT });
  try {
    return JSON.parse(out);
  } catch {
    return { error: out };
  }
}

export async function captureEnvironment({ servers, titiler, store, cogNames, args, outDir, titilerWorkers }) {
  const pkg = JSON.parse(await readFile(path.join(REPO_ROOT, 'bench/package.json'), 'utf8'));
  const lock = JSON.parse(await readFile(path.join(REPO_ROOT, 'bench/package-lock.json'), 'utf8'));
  const installed = (name) => lock.packages?.[`node_modules/${name}`]?.version ?? null;
  const playwright = JSON.parse(await readFile(path.join(REPO_ROOT, 'bench/node_modules/playwright/package.json'), 'utf8')).version;
  const git = { commit: await text('git', ['rev-parse', 'HEAD'], { cwd: REPO_ROOT }), branch: await text('git', ['rev-parse', '--abbrev-ref', 'HEAD'], { cwd: REPO_ROOT }), dirty: (await text('git', ['status', '--porcelain', '--', 'bench', 'js', 'src'], { cwd: REPO_ROOT })) !== '' };
  const attrs = store.attributes.chronozarr;
  const info = titiler ? await fetch(`${titiler.url}/cog/info?url=${encodeURIComponent(servers.cogUrl(START_T))}`).then((r) => r.json()).catch((e) => ({ error: String(e) })) : null;

  return {
    capturedAt: new Date().toISOString(),
    command: `node bench/v03/run.mjs ${args.join(' ')}`.trim(),
    machine: {
      model: await text('sysctl', ['-n', 'machdep.cpu.brand_string']),
      cpus: os.cpus().length,
      memoryGB: Math.round(os.totalmem() / 2 ** 30),
      os: `${await text('sw_vers', ['-productName'])} ${await text('sw_vers', ['-productVersion'])} (${os.release()})`,
      uptimeAtStart: await text('uptime', []),
      loadavgAtStart: os.loadavg(),
    },
    node: process.version,
    python: await pythonInfo(),
    playwright,
    browser: await browserInfo(servers),
    browserSettings: { channel: 'chromium (new headless)', args: ['--ignore-gpu-blocklist', '--use-angle=metal'], viewport: VIEWPORT, httpCache: 'disabled through CDP Network.setCacheDisabled', serviceWorkers: 'blocked' },
    libraries: {
      'maplibre-gl': { declared: pkg.dependencies['maplibre-gl'], installed: installed('maplibre-gl') },
      '@carbonplan/zarr-layer': { declared: pkg.dependencies['@carbonplan/zarr-layer'], installed: installed('@carbonplan/zarr-layer') },
      proj4: installed('proj4'),
      zarrita: installed('zarrita'),
      chronozarrReader: 'js/chronozarr (this repository, commit below)',
    },
    docker: {
      client: await text('docker', ['version', '--format', '{{.Client.Version}}']),
      server: await text('docker', ['version', '--format', '{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}']),
      vm: await text('docker', ['info', '--format', '{{.NCPU}} CPUs, {{.MemTotal}} bytes memory, {{.OperatingSystem}}, kernel {{.KernelVersion}}']),
    },
    titiler: titiler && { ...(await titilerImageInfo()), health: titiler.health, workers: titilerWorkers, containerEnv: { ...TITILER.env, WEB_CONCURRENCY: String(titilerWorkers) }, tileQuery: TITILER_TILE_QUERY, cogInfoSample: info },
    git,
    data: {
      store: { dir: STORE_DIR, specVersion: attrs.spec_version, timesteps: attrs.times.length, firstTime: attrs.times[0], lastTime: attrs.times.at(-1), bands: attrs.band_names, levels: attrs.levels.map((l) => ({ path: l.path, resolution: l.resolution, shape: l.shape, grid: l.grid })) },
      cogs: { dir: COG_DIR, files: cogNames.length, totalBytes: await dirSize(COG_DIR, cogNames), exporter: 'chronozarr export-cog --level 0 (GDAL COG driver, DEFLATE, 512 px tiles, overviews 2/4/8)' },
    },
    protocol: { startT: START_T, steps: STEPS, jumpDistance: JUMP_DISTANCE, thinkMs: THINK_MS, trailingIdleMs: TRAILING_IDLE_MS, rafNominalMs: RAF_NOMINAL_MS, command: args },
    outDir,
  };
}
