// Play on a link that cannot carry the speed: the viewer opens a store on an unthrottled link, CDP then limits the link,
// and Play is pressed at 30 steps/s. One JSON line: the first step, steps and pauses in the window, bytes and requests,
// and the playback object sampled every second (steps, `b` while buffering, @ the speed it delivers).
//   node bench/sar/throttled.mjs <label> <sentinel1|nisar> <Mbit/s> <seconds>
// Run it from the tree to measure (a git archive of another commit with these scripts copied in works: see merge-main.md).
import {startServer, launchBrowser, newPage, attachNet, applyProfile} from '../lib/harness.mjs';

const [label, sensor, mbit, seconds] = process.argv.slice(2);
const stores = {sentinel1: 'ucayali_santa_maria/s1-rtc-monthly-v1', nisar: 'colorado_yuma/nisar-gcov-v2'};
if (!label || !stores[sensor] || !(Number(mbit) > 0) || !(Number(seconds) > 0)) {
  console.error('Usage: node bench/sar/throttled.mjs <label> <sentinel1|nisar> <Mbit/s> <seconds>');
  process.exit(2);
}
const server = await startServer();
const browser = await launchBrowser({channel: 'chromium'});
try {
  const {page, context} = await newPage(browser, {width: 1440, height: 900, deviceScaleFactor: 2, blockCatalog: true});
  const url = `${server.url}/data/stores/${stores[sensor]}`;
  const {cdp, counter} = await attachNet(context, page, [url]);
  await page.goto(`${server.url}/js/demo/index.html?store=${encodeURIComponent(url)}`);
  await page.waitForFunction(() => window.chronozarr?.ready, null, {timeout: 60000});
  await applyProfile(cdp, {mbps: Number(mbit), latencyMs: 20});
  const before = counter.snapshot();
  const result = await page.evaluate(async (seconds) => {
    const v = window.chronozarr.viewer;
    await window.chronozarr.ready;
    await v.loadStore(v.store.url, {lod: 0, fetch: (input, init) => fetch(input, {...init, cache: 'no-store'})});
    while (!(v.shownFrame?.lod === 0 && v.shownFrame?.kind === 'target')) await new Promise((resolve) => setTimeout(resolve, 20));
    v.playback.setSpeed(30);
    const start = performance.now();
    v.play();
    const perSecond = [];
    while (performance.now() - start < seconds * 1000) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const p = v.playback;
      perSecond.push(`${p.stats.steps}${p.buffering ? 'b' : ''}@${p.effectiveStepsPerSecond.toFixed(2)}`);
    }
    const stats = {...v.playback.stats};
    v.pause();
    return {steps: stats.steps, firstStepMs: stats.firstStepAt === null ? null : Math.round(stats.firstStepAt - start), pauses: stats.bufferingPauses, pausedMs: Math.round(stats.bufferingMs), longestPauseMs: Math.round(stats.longestBufferingMs), perSecond};
  }, Number(seconds));
  const after = counter.snapshot();
  console.log(JSON.stringify({label, sensor, mbit: Number(mbit), seconds: Number(seconds), ...result, MiB: Math.round((after.bytes - before.bytes) / 1048576), requests: after.requests - before.requests, perSecond: result.perSecond.join(' ')}));
} finally {
  await browser.close();
  await server.close();
}
