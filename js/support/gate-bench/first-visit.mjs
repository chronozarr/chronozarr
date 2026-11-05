// The first visit to the viewer, measured from outside: a new browser with an empty profile and the HTTP cache on loads the
// page, which opens the store named in its address. The gates (run.mjs) run on a page whose workers, shader programs and
// connections are warm from the opens before, so they cannot see what the first open pays for. What this reports, in ms, none
// of it gated:
//
//   firstRequestMs   from the start of the navigation to the request of the first chunk (CDP's network events)
//   shaderMs         the time the page spent blocked in WebGL's compileShader, linkProgram and the status reads that wait for them
//   workerReadyMs    from `new Worker` to the first message of a decode worker (the median of the workers), which says it is
//                    ready once its script and its codec have loaded
//   codecMs          from the first request of a codec module by the page to the first copy of it that finished (the workers'
//                    own requests are not in the page's CDP log; workerReadyMs holds them)
//   firstFrameMs     from the start of the navigation to the first whole frame, at any level
//   targetFrameMs    from the start of the navigation to the first whole frame at the level the viewer wants
//
// The page is instrumented from outside, by an init script that runs before the viewer's: it wraps the WebGL and Worker
// constructors and listens to the viewer's `probe`. A change to the viewer that renames what the script wraps leaves the
// marks null, not wrong.

import { LOOPBACK_ONLY, VIEWPORT, launchBrowser, withTimeout } from './browser.mjs';
import { attachNetworkLog } from './cdp.js';
import { visitMarks } from './visit-marks.js';

const VISIT_TIMEOUT_MS = 60_000;
// The workers say they are ready after the first frame when the page was quick: wait for them this long.
const WORKER_WAIT_MS = 3000;

/** In the page, before any script: collect the marks into `window.__firstVisit`. */
function installMarks() {
  const marks = { shaderMs: 0, programs: 0, workers: [], firstFrameMs: null, targetFrameMs: null };
  window.__firstVisit = marks;

  // The viewer reads the compile and link status right after compiling and linking: that read is where the driver blocks.
  const gl = WebGL2RenderingContext.prototype;
  for (const name of ['compileShader', 'linkProgram', 'getShaderParameter', 'getProgramParameter']) {
    const original = gl[name];
    gl[name] = function timed(...args) {
      const started = performance.now();
      try {
        return original.apply(this, args);
      } finally {
        marks.shaderMs += performance.now() - started;
        if (name === 'linkProgram') marks.programs++;
      }
    };
  }

  const NativeWorker = window.Worker;
  window.Worker = class extends NativeWorker {
    constructor(url, options) {
      const spawnedAt = performance.now();
      super(url, options);
      const worker = { spawnedAt, firstMessageAt: null };
      marks.workers.push(worker);
      this.addEventListener('message', () => (worker.firstMessageAt ??= performance.now()), { once: true });
    }
  };

  // The viewer sets window.chronozarr once, before it opens a store: take the viewer from there and listen for its first frames.
  Object.defineProperty(window, 'chronozarr', {
    configurable: true,
    get: () => undefined,
    set(value) {
      Object.defineProperty(window, 'chronozarr', { value, writable: true, configurable: true, enumerable: true });
      value.viewer.probe = (event) => {
        if (event.type !== 'paint') return;
        marks.firstFrameMs ??= event.at;
        if (event.kind === 'target') marks.targetFrameMs ??= event.at;
      };
    },
  });
}

/** One first visit: a new browser, the viewer at `appUrl` opening the store at `storeUrl`, and its marks. */
export async function firstVisit({ gpu, appUrl, storeUrl }) {
  const browser = await launchBrowser(gpu, [LOOPBACK_ONLY]);
  try {
    const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1, serviceWorkers: 'block' });
    const page = await context.newPage();
    await page.addInitScript(installMarks);
    // The cache stays on, as a visitor's does: its first visit has nothing in it, but a worker and the main thread share a codec download.
    const { log } = await attachNetworkLog(context, page, [new URL(appUrl).origin, new URL(storeUrl).origin], { cacheDisabled: false });
    await withTimeout(
      (async () => {
        await page.goto(`${appUrl}/demo/index.html?store=${encodeURIComponent(storeUrl)}`);
        await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: VISIT_TIMEOUT_MS });
        if (!(await page.evaluate(() => window.chronozarr.ready))) throw new Error('the viewer did not open the store');
        // A worker that has not spoken by then leaves workerReadyMs to those that have.
        await page
          .waitForFunction(() => window.__firstVisit.workers.every((worker) => worker.firstMessageAt !== null), null, { timeout: WORKER_WAIT_MS })
          .catch((error) => {
            if (error.name !== 'TimeoutError') throw error;
          });
      })(),
      'the first visit',
      VISIT_TIMEOUT_MS,
    );
    return visitMarks(await page.evaluate(() => window.__firstVisit), log.entries(), new URL(storeUrl).origin);
  } finally {
    await browser.close();
  }
}
