// Playwright fixtures: a static server for the site (js/ as the web root, as on Cloudflare), a second one for the
// synthetic stores written to a temp directory (a separate origin, as data.tileripper.com is), the check that
// the browser has WebGL2, and the rule that a test fails on any console error or on a request leaving the machine.

import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test as base, expect } from '@playwright/test';
import { startStaticServer } from '../support/static-server.js';
import { STORES, writeStore } from './stores.js';

const JS_DIR = path.resolve(import.meta.dirname, '..');
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost']);

/** What a WebGL2 context of this browser reports, or null when it cannot be created. */
export function probeWebGL2() {
  const gl = document.createElement('canvas').getContext('webgl2');
  if (!gl) return null;
  const debug = gl.getExtension('WEBGL_debug_renderer_info');
  return {
    version: gl.getParameter(gl.VERSION),
    renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
    maxArrayTextureLayers: gl.getParameter(gl.MAX_ARRAY_TEXTURE_LAYERS),
    maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE),
  };
}

export const test = base.extend({
  // Fails every test of the worker, with the browser version and flags in the message, when there is no WebGL2.
  webgl: [
    async ({ browser, launchOptions }, use) => {
      const context = await browser.newContext();
      const info = await (await context.newPage()).evaluate(probeWebGL2);
      await context.close();
      if (!info) {
        throw new Error(
          `WebGL2 is not available in headless ${browser.browserType().name()} ${browser.version()} (launch args: ${JSON.stringify(launchOptions.args)}). ` +
            'The viewer cannot render without it: on a Linux runner check that the browser was installed with --with-deps and that SwiftShader is not disabled.',
        );
      }
      await use(info);
    },
    { scope: 'worker', auto: true },
  ],

  // A temp directory the stores are written to on first use, removed when the worker ends.
  stores: [
    async ({}, use) => {
      const dir = await mkdtemp(path.join(tmpdir(), 'chronozarr-stores-'));
      const written = new Map();
      await use({
        dir,
        ensure(name) {
          if (!STORES[name]) throw new Error(`unknown store "${name}"; known: ${Object.keys(STORES).join(', ')}`);
          if (!written.has(name)) written.set(name, writeStore(dir, name));
          return written.get(name);
        },
      });
      await rm(dir, { recursive: true, force: true });
    },
    { scope: 'worker' },
  ],

  servers: [
    async ({ stores }, use) => {
      const app = await startStaticServer(JS_DIR);
      const data = await startStaticServer(stores.dir);
      await use({ app, data, appUrl: app.url, dataUrl: data.url });
      await Promise.all([app.close(), data.close()]);
    },
    { scope: 'worker' },
  ],

  /** (name) => base URL of the synthetic store `name`, generating it first. */
  storeUrl: async ({ stores, servers }, use) => {
    await use(async (name) => {
      await stores.ensure(name);
      return `${servers.dataUrl}/${name}/`;
    });
  },

  // Anything that is not the two local servers is refused, so a test never reaches the network by accident (a
  // viewer that fell back to the catalog store would otherwise pass against the real site).
  page: async ({ page }, use) => {
    await page.route(
      (url) => !LOCAL_HOSTS.has(url.hostname),
      (route) => route.abort('blockedbyclient'),
    );
    await use(page);
  },

  consoleErrors: [
    async ({ page }, use) => {
      const errors = [];
      page.on('console', (message) => {
        if (message.type() === 'error') errors.push(`console.error: ${message.text()}`);
      });
      page.on('pageerror', (error) => errors.push(`uncaught: ${error.stack || error.message}`));
      await use(errors);
      expect(errors, 'the page logged errors').toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
