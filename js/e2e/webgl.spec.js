import { probeWebGL2, expect, test } from './fixtures.js';

test('headless Chromium creates a WebGL2 context on software GL', async ({ page, webgl }, testInfo) => {
  await page.goto('about:blank');
  const info = await page.evaluate(probeWebGL2);
  expect(info, 'WebGL2 context').not.toBeNull();
  expect(info.version).toMatch(/WebGL 2\.0/);
  // The viewer packs chunks into one texture array: a chunk is nBand layers, and the pool needs at least a few chunks.
  expect(info.maxArrayTextureLayers).toBeGreaterThanOrEqual(256);
  expect(webgl.renderer).toBe(info.renderer);
  testInfo.annotations.push({ type: 'webgl2', description: `${info.version}; ${info.renderer}; ${info.maxArrayTextureLayers} array layers; max texture ${info.maxTextureSize}` });
});
