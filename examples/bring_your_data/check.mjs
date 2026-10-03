// Check the independently hosted bundle. Run after starting serve.py.
import assert from 'node:assert/strict';
import { chromium } from '../../js/node_modules/playwright/index.mjs';

const root = new URL(process.argv[2] ?? 'http://127.0.0.1:8000/my-published-series/');
if (!root.pathname.endsWith('/')) root.pathname += '/';
const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
try {
  const page = await browser.newPage();
  const errors = [], external = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (['http:', 'https:'].includes(url.protocol) && url.origin !== root.origin) {
      external.push(url.href);
      return route.abort();
    }
    return route.continue();
  });
  await page.goto(new URL('index.html', root).href);
  await page.waitForFunction(() => window.chronozarr?.ready);
  await page.evaluate(() => window.chronozarr.ready);
  await page.waitForFunction(() => window.chronozarr.viewer.renderNow().complete);
  const times = await page.evaluate(() => window.chronozarr.viewer.store.times.length);
  assert.ok(times >= 2, 'Use at least two observations to verify time controls.');
  await page.goto(new URL('examples/embed.html', root).href);
  await page.waitForFunction(() => !document.querySelector('#slider').disabled);
  const frame = page.frames().find(frame => frame.url().includes('/demo/'));
  assert.ok(frame, 'The embedded viewer loaded.');
  await page.locator('#slider').fill('1');
  await page.locator('#slider').dispatchEvent('input');
  await frame.waitForFunction(() => window.chronozarr.viewer.paintedT === 1 && window.chronozarr.viewer.renderNow().complete);
  const options = await page.locator('#product option').evaluateAll(options => options.map(option => option.value));
  const product = options.find(id => id === 'ndvi') ?? options.at(-1);
  await page.locator('#product').selectOption(product);
  await frame.waitForFunction(id => window.chronozarr.viewer.products[window.chronozarr.viewer.productIndex]?.id === id, product);
  await frame.locator('#gl-canvas').click();
  await page.waitForFunction(() => document.querySelector('#click dl') !== null);
  await page.locator('#play').click();
  await frame.waitForFunction(() => window.chronozarr.viewer.playback.playing);
  await page.locator('#pause').click();
  await frame.waitForFunction(() => !window.chronozarr.viewer.playback.playing);
  assert.deepEqual(external, [], 'The viewer and embed must not request external resources.');
  assert.deepEqual(errors, [], 'The browser must not report errors.');
  console.log(JSON.stringify({ url: root.href, times, fullViewer: 'passed', embedTimeControl: 'passed', productControl: 'passed', pixelClick: 'passed', playbackControls: 'passed', externalRequests: external.length, browserErrors: errors.length }, null, 2));
} finally {
  await browser.close();
}
