import { expect, test } from './fixtures.js';
import { openViewer } from './viewer-helpers.js';

const timeIndex = (page) => page.evaluate(() => window.chronozarr.viewer.t);

test.describe('keyboard', () => {
  test('the timeline is a slider: it takes focus, follows the time, and Home, End and the arrows move it', async ({ page, servers, storeUrl }) => {
    await openViewer(page, servers, await storeUrl('u16_plain'));
    const track = page.locator('#timeline-track');
    const last = (await page.evaluate(() => window.chronozarr.viewer.store.times.length)) - 1;
    await expect(track).toHaveAttribute('role', 'slider');
    await expect(track).toHaveAttribute('aria-valuemax', String(last));
    await expect(track).toHaveAttribute('aria-valuenow', '0');

    await track.focus();
    await expect(track).toBeFocused();
    await page.keyboard.press('ArrowRight');
    await expect(track).toHaveAttribute('aria-valuenow', '1');
    await page.keyboard.press('ArrowUp');
    await expect(track).toHaveAttribute('aria-valuenow', '2');
    await page.keyboard.press('ArrowDown');
    await expect(track).toHaveAttribute('aria-valuenow', '1');
    await page.keyboard.press('End');
    await expect(track).toHaveAttribute('aria-valuenow', String(last));
    await page.keyboard.press('ArrowRight');
    await expect(track, 'the end of the timeline stays the end').toHaveAttribute('aria-valuenow', String(last));
    await page.keyboard.press('Home');
    await expect(track).toHaveAttribute('aria-valuenow', '0');
    expect(await timeIndex(page)).toBe(0);

    await page.keyboard.press('End');
    const label = (await page.locator('#time-label').textContent()).trim();
    await expect(track, 'a screen reader reads the date, not the index').toHaveAttribute('aria-valuetext', label);
  });

  test('Space on a focused button presses that button; anywhere else it plays and pauses', async ({ page, servers, storeUrl }) => {
    await openViewer(page, servers, await storeUrl('u16_plain'));
    const play = page.locator('#play-btn');

    await page.focus('#next-btn');
    await page.keyboard.press('Space');
    await expect.poll(() => timeIndex(page), 'Space on the next button steps').toBe(1);
    await expect(play, 'and does not start playback').toHaveAttribute('aria-label', 'Play (Space)');

    await page.focus('#play-btn');
    await page.keyboard.press('Space');
    await expect(play, 'Space on the play button plays once').toHaveAttribute('aria-label', 'Pause (Space)');
    await page.keyboard.press('Space');
    await expect(play).toHaveAttribute('aria-label', 'Play (Space)');

    await page.locator('#timeline-track').focus();
    await page.keyboard.press('Space');
    await expect(play, 'Space on the timeline starts playback').toHaveAttribute('aria-label', 'Pause (Space)');
    await page.keyboard.press('Space');
    await expect(play).toHaveAttribute('aria-label', 'Play (Space)');
  });

  test('the controls have names and every stop in the tab order shows a focus ring', async ({ page, servers, storeUrl }) => {
    await openViewer(page, servers, await storeUrl('u16_plain'));
    const stops = [];
    for (let i = 0; i < 20; i++) {
      await page.keyboard.press('Tab');
      const stop = await page.evaluate(() => {
        const el = document.activeElement;
        if (!el || el === document.body) return null;
        const style = getComputedStyle(el);
        const name = el.getAttribute('aria-label') || el.labels?.[0]?.textContent.trim() || el.textContent.trim() || el.title;
        return { id: el.id || el.tagName, name, ring: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0 };
      });
      if (!stop) break;
      stops.push(stop);
    }
    expect(stops.map((s) => s.id), 'the product buttons, the transport controls, the timeline, the speed and the export button are all reachable').toEqual(
      expect.arrayContaining(['play-btn', 'prev-btn', 'next-btn', 'timeline-track', 'speed', 'export-btn']),
    );
    for (const stop of stops) {
      expect(stop.name, `${stop.id} has an accessible name`).toBeTruthy();
      expect(stop.ring, `${stop.id} shows a focus ring`).toBe(true);
    }
  });
});

test.describe('a phone: 375 x 812, touch', () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true, deviceScaleFactor: 3 });

  test('the controls are 44 px targets and the page does not scroll sideways', async ({ page, servers, storeUrl }) => {
    await openViewer(page, servers, await storeUrl('u16_plain'));
    const sizes = await page.evaluate(() =>
      Object.fromEntries(
        ['play-btn', 'prev-btn', 'next-btn', 'export-btn', 'timeline-track', 'product-select', 'speed'].map((id) => {
          const rect = document.getElementById(id).getBoundingClientRect();
          return [id, [Math.round(rect.width), Math.round(rect.height)]];
        }),
      ),
    );
    for (const [id, [width, height]] of Object.entries(sizes)) {
      expect(width, `${id} is at least 44 px wide`).toBeGreaterThanOrEqual(44);
      expect(height, `${id} is at least 44 px high`).toBeGreaterThanOrEqual(44);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expect(page.locator('.hint-touch')).toBeVisible();
    await expect(page.locator('.hint-pointer')).toBeHidden();
  });

  test('a finger dragged along the timeline scrubs it', async ({ page, servers, storeUrl }) => {
    await openViewer(page, servers, await storeUrl('u16_plain'));
    const box = await page.locator('#timeline-track').boundingBox();
    const y = box.y + box.height / 2;
    const cdp = await page.context().newCDPSession(page);
    const touch = (type, x) => cdp.send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' ? [] : [{ x, y, id: 1 }] });
    await touch('touchStart', box.x + 12);
    await touch('touchMove', box.x + box.width / 2);
    await touch('touchMove', box.x + box.width - 12);
    await touch('touchEnd');
    const last = (await page.evaluate(() => window.chronozarr.viewer.store.times.length)) - 1;
    await expect.poll(() => timeIndex(page)).toBe(last);
  });
});
