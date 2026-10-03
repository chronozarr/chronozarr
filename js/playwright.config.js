// Browser tests of the viewer and the MapLibre layer in headless Chromium with software WebGL (SwiftShader).
// `npx playwright test` from js/; the specs, stores and servers are in e2e/. The report and traces go to
// PLAYWRIGHT_ARTIFACTS_DIR (default: the system temp directory), outside the tree that is deployed as the site.

import os from 'node:os';
import path from 'node:path';
import { defineConfig, devices } from '@playwright/test';

const artifacts = process.env.PLAYWRIGHT_ARTIFACTS_DIR ?? path.join(os.tmpdir(), 'chronozarr-playwright');
const ci = Boolean(process.env.CI);

export default defineConfig({
  testDir: './e2e',
  testMatch: '*.spec.js',
  outputDir: path.join(artifacts, 'results'),
  forbidOnly: ci,
  retries: ci ? 1 : 0,
  workers: ci ? 2 : undefined,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: ci ? [['list'], ['html', { outputFolder: path.join(artifacts, 'report'), open: 'never' }]] : [['list']],
  use: {
    ...devices['Desktop Chrome'],
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 1,
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: {
      // WebGL2 on the CPU. The chromium headless shell falls back to SwiftShader by itself on most machines; the flags
      // make it explicit, and the webgl fixture fails every test if the context still cannot be created.
      args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
    },
  },
  projects: [{ name: 'chromium' }],
});
