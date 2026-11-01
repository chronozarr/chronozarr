// Import the deployed module graph from another origin through the real local asset handler.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { copyFile, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';

const site = fileURLToPath(new URL('..', import.meta.url));
const repo = resolve(site, '..');
const require = createRequire(import.meta.resolve('vocs'));
const { parse } = require('toml');
const { chromium } = createRequire(join(repo, 'js/package.json'))('playwright');
const scratch = await mkdtemp(join(tmpdir(), 'chronozarr-module-cors-'));
const consumer = createServer((_request, response) => {
  response.writeHead(200, { 'content-type': 'text/html' });
  response.end('<!doctype html><title>External module consumer</title>');
});
const listen = server => new Promise((resolve, reject) => {
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => resolve(server.address().port));
});
const close = server => new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
let worker;
let exited;
let browser;
let logs = '';
try {
  const config = parse(await readFile(join(site, 'wrangler.toml'), 'utf8'));
  delete config.routes;
  delete config.workers_dev;
  config.name = 'chronozarr-hosting-check';
  config.main = 'worker.js';
  config.assets.directory = resolve(site, config.assets.directory);
  await Promise.all(['worker.js', 'redirects.json'].map(name => copyFile(join(site, name), join(scratch, name))));
  const configPath = join(scratch, 'wrangler.json');
  await writeFile(configPath, JSON.stringify(config));
  const reservation = createServer();
  const port = await listen(reservation);
  await close(reservation);
  const origin = `http://127.0.0.1:${port}`;
  worker = spawn(process.execPath, [
    join(repo, 'node_modules/wrangler/bin/wrangler.js'), 'dev', '--local', '--config', configPath,
    '--port', String(port), '--ip', '127.0.0.1', '--inspector-port', '0',
    '--persist-to', join(scratch, 'state'), '--log-level', 'error',
  ], {
    cwd: scratch,
    env: { ...process.env, WRANGLER_SEND_METRICS: 'false', XDG_CONFIG_HOME: join(scratch, 'config') },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  exited = new Promise(resolve => worker.once('exit', resolve));
  for (const stream of [worker.stdout, worker.stderr]) stream.on('data', chunk => { logs = `${logs}${chunk}`.slice(-6000); });
  let ready = false;
  for (let attempt = 0; attempt < 300; attempt++) {
    if (worker.exitCode !== null) throw new Error(`Local asset handler exited: ${logs}`);
    try { ready = (await fetch(`${origin}/maplibre/layer.js`)).ok; } catch { /* Starting. */ }
    if (ready) break;
    await delay(100);
  }
  assert.ok(ready, `Local asset handler did not start: ${logs}`);
  const consumerOrigin = `http://127.0.0.1:${await listen(consumer)}`;
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.goto(consumerOrigin);
  for (const entry of ['maplibre/layer.js', 'geolibre/plugin.js', 'chronozarr/decoder.js']) {
    const exports = await page.evaluate(async url => {
      const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('Module import timed out')), 15000));
      return Object.keys(await Promise.race([import(url), timeout]));
    }, `${origin}/${entry}`).catch(cause => {
      throw new Error(`${entry} failed to import across origins: ${errors.join('\n')}`, { cause });
    });
    assert.ok(exports.length, `${entry} has no module exports`);
  }
  assert.deepEqual(errors, [], `Cross-origin imports reported browser errors: ${errors.join('\n')}`);
  console.log('Hosted reader, MapLibre and GeoLibre module graphs import across origins.');
} finally {
  await browser?.close();
  if (consumer.listening) await close(consumer);
  if (worker?.exitCode === null) {
    worker.kill('SIGINT');
    await Promise.race([exited, delay(2000)]);
    if (worker.exitCode === null) worker.kill('SIGKILL');
    await exited;
  }
  await rm(scratch, { recursive: true, force: true });
}
