// What the runners of this directory (run.mjs, link.mjs, first-visit.mjs) share about their browser.

import { chromium } from '@playwright/test';

export const BROWSER_ARGS = {
  // The flags of playwright.config.js: WebGL2 on the CPU, the same on a laptop as on a runner without a GPU.
  swiftshader: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
  // Those of scripts/fieldperf/lib.mjs.
  metal: ['--ignore-gpu-blocklist', '--use-angle=metal'],
};
export const VIEWPORT = { width: 1280, height: 720 };

export const launchBrowser = (gpu, extraArgs = []) => chromium.launch({ headless: true, args: [...BROWSER_ARGS[gpu], ...extraArgs] });

/** In the page: the browser and the WebGL renderer it draws with. */
export function rendererInfo() {
  const gl = document.createElement('canvas').getContext('webgl2');
  const debug = gl?.getExtension('WEBGL_debug_renderer_info');
  return { userAgent: navigator.userAgent, renderer: gl ? gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER) : null };
}

export function withTimeout(promise, what, ms) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} took more than ${ms / 1000} s`)), ms);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}
