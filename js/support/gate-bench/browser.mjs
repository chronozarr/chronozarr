// What the runners of this directory (run.mjs, link.mjs, first-visit.mjs) share about their browser.

import { chromium } from '@playwright/test';

export const BROWSER_ARGS = {
  // The flags of playwright.config.js: WebGL2 on the CPU, the same on a laptop as on a runner without a GPU.
  swiftshader: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
  // Those of scripts/fieldperf/lib.mjs.
  metal: ['--ignore-gpu-blocklist', '--use-angle=metal'],
};
export const VIEWPORT = { width: 1280, height: 720 };
// Only the loopback is reachable: a request to anything else fails to resolve at once, with no interception round trip in Node
// (a Playwright route sends every request through Node, which adds 40 to 60 ms to a cold frame when it fulfils them).
export const LOOPBACK_ONLY = '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1';

export const launchBrowser = (gpu, extraArgs = []) => chromium.launch({ headless: true, args: [...BROWSER_ARGS[gpu], ...extraArgs] });

/** In the page: the browser and the WebGL renderer it draws with. */
export function rendererInfo() {
  const gl = document.createElement('canvas').getContext('webgl2');
  const debug = gl?.getExtension('WEBGL_debug_renderer_info');
  return { userAgent: navigator.userAgent, renderer: gl ? gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER) : null };
}

/** In the page: the median time between `frames` animation frames, in ms. */
export function frameIntervalMedian(frames) {
  return new Promise((resolve) => {
    const gaps = [];
    let last = null;
    const tick = (now) => {
      if (last !== null) gaps.push(now - last);
      last = now;
      if (gaps.length < frames) requestAnimationFrame(tick);
      else resolve([...gaps].sort((a, b) => a - b)[Math.floor(gaps.length / 2)]);
    };
    requestAnimationFrame(tick);
  });
}

/** The median animation frame interval of `browser`, in ms, on a blank page of its own: the viewer's page is busy with its own work. */
export async function measureFrameInterval(browser) {
  const page = await browser.newPage();
  try {
    return await withTimeout(page.evaluate(frameIntervalMedian, 60), 'measuring the animation frame interval', 15_000);
  } finally {
    await page.close();
  }
}

export function withTimeout(promise, what, ms) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} took more than ${ms / 1000} s`)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}
