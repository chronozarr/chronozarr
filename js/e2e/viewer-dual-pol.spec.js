import { formatValue } from '../demo/inspector.js';
import { expect, test } from './fixtures.js';
import { STORES, storedValue } from './stores.js';
import { captureFrame, clickStorePixel, expectFrameMatchesStore, openViewer, readProductButtons, readSidebar, waitForPaintedTime } from './viewer-helpers.js';

const STORE = 'u16_sar';
const physical = (stored) => stored / 100 - 60;

/** [min, max] of `fn(t, y, x)` over every timestep and pixel of the store, masked ones included: a bound on what the viewer samples. */
function bounds(fn) {
  const { spec } = STORES[STORE];
  let [lo, hi] = [Infinity, -Infinity];
  for (let t = 0; t < spec.nTime; t++) {
    for (let y = 0; y < spec.height; y++) {
      for (let x = 0; x < spec.width; x++) {
        const value = fn(t, y, x);
        lo = Math.min(lo, value);
        hi = Math.max(hi, value);
      }
    }
  }
  return [lo, hi];
}

test('HH and HV open on the dual-pol composite, drawn as the CPU render of the same values, with ranges that stay put through time', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl(STORE));

  const buttons = await readProductButtons(page);
  expect(buttons.filter(([, enabled]) => enabled).map(([name]) => name), 'enabled product buttons').toEqual(STORES[STORE].enabledProducts);
  expect(buttons.filter(([, , active]) => active).map(([name]) => name), 'the composite is the first view').toEqual(['Dual-pol composite']);
  await expect(page.locator('#stretch')).toBeHidden();

  const first = await captureFrame(page);
  expectFrameMatchesStore(first, STORE, 0);
  expect(first.frame.shader).toBe(6);
  expect(first.frame.range, 'one [lo, hi] for each of the three channels').toHaveLength(3);

  // Every channel's range lies inside the values the store holds: co-pol, cross-pol and their difference in dB.
  const hh = (t, y, x) => physical(storedValue(STORE, t, 0, y, x));
  const hv = (t, y, x) => physical(storedValue(STORE, t, 1, y, x));
  const stored = [bounds(hh), bounds(hv), bounds((t, y, x) => hh(t, y, x) - hv(t, y, x))];
  first.frame.range.forEach(([lo, hi], channel) => {
    expect(hi, `channel ${channel} is not degenerate`).toBeGreaterThan(lo);
    expect(lo, `channel ${channel} lo`).toBeGreaterThanOrEqual(stored[channel][0] - 1e-6);
    expect(hi, `channel ${channel} hi`).toBeLessThanOrEqual(stored[channel][1] + 1e-6);
  });

  // Stepping through time repaints with the same ranges, and every frame still equals the CPU render.
  for (const t of [1, 2, 3]) {
    await page.keyboard.press('ArrowRight');
    await waitForPaintedTime(page, t);
    const capture = await captureFrame(page);
    expectFrameMatchesStore(capture, STORE, t);
    expect(capture.frame.range, `ranges at t=${t}`).toEqual(first.frame.range);
  }
});

test('the single band of a SAR store stays available with its own adjustable range, and the composite measures its ranges again when chosen', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl(STORE));
  await page.locator('#products button', { hasText: /^Single band$/ }).click();
  await expect(page.locator('#products button.active')).toHaveText('Single band');
  await expect(page.locator('#stretch')).toBeVisible();
  const single = await captureFrame(page);
  expectFrameMatchesStore(single, STORE, 0);
  expect(single.frame.shader).toBe(5);
  expect(single.frame.range).toHaveLength(2);

  await page.locator('#products button', { hasText: /^Dual-pol composite$/ }).click();
  await expect(page.locator('#products button.active')).toHaveText('Dual-pol composite');
  await expect(page.locator('#stretch')).toBeHidden();
  const again = await captureFrame(page);
  expectFrameMatchesStore(again, STORE, 0);
  expect(again.frame.range).toHaveLength(3);
});

test('the inspector shows both bands in dB and the chart has the co-pol, cross-pol and ratio lines', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl(STORE));
  const [X, Y] = [150, 120];
  await clickStorePixel(page, X, Y);
  await expect(page.locator('#sidebar-content')).toContainText(`${X}, ${Y}`);
  const sidebar = await readSidebar(page);
  const [hh, hv] = [0, 1].map((band) => storedValue(STORE, 0, band, Y, X));
  const dB = (value) => `${formatValue(physical(value))} dB`;
  expect(sidebar.Value).toEqual([['HH', dB(hh)], ['HV', dB(hv)]]);
  expect(sidebar.Indices, 'no optical index for a SAR store').toBeUndefined();
  await expect(page.locator('#chart-legend')).toContainText('HH co-pol');
  await expect(page.locator('#chart-legend')).toContainText('HV cross-pol');
  await expect(page.locator('#chart-legend')).toContainText('co/cross ratio');
});
