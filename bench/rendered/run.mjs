import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { build } from "esbuild";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import { startServer, launchBrowser, newPage, attachNet, subtract, applyProfile, PROFILES, NetCounter } from "../lib/harness.mjs";
await mkdir(new URL("./dist", import.meta.url), { recursive: true });
await build({ entryPoints: [new URL("./page.js", import.meta.url).pathname], bundle: true, external: ["/js/*", "/bench/rendered/dist/*"], format: "esm", target: "es2022", outfile: new URL("./dist/page.js", import.meta.url).pathname });
await build({ entryPoints: [new URL("./cog-entry.js", import.meta.url).pathname], bundle: true, format: "esm", target: "es2022", outfile: new URL("./dist/cog.js", import.meta.url).pathname });
const inputFiles = ["input-full/source.json", "input-full/observations.csv", "input-full/2020-05-05.tif", "input-full/2020-06-09.tif", "input-full/2020-07-29.tif", "series/zarr.json", "plain-zarr/zarr.json"];
const inputHashes = {};
for (const file of inputFiles) {
  inputHashes[file] = createHash("sha256").update(await readFile(new URL("../../data/adoption/lake-mead-2020/" + file, import.meta.url))).digest("hex");
}
const scriptHashes = {};
for (const file of ["page.js", "run.mjs", "cog-entry.js", "../../js/demo/renderer.js", "../../js/demo/products-glsl.js", "../lib/harness.mjs"]) scriptHashes[file] = createHash("sha256").update(await readFile(new URL(file, import.meta.url))).digest("hex");
const dependencies = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
const server = await startServer(), browser = await launchBrowser(), runs = [];
try {
  for (const profile of ["natural", "50Mbit-40ms"]) for (let rep = 0; rep < 3; rep++) for (let i = 0; i < 3; i++) {
    const kind = ["chronozarr", "zarr", "cog"][(i + rep) % 3];
    const { context, page } = await newPage(browser);
    await page.goto(server.url + "/bench/rendered/page.html");
    await page.waitForFunction(() => window.run);
    const { counter, cdp } = await attachNet(context, page, [server.url + "/data/adoption/"]);
    const totalCounter = new NetCounter(cdp, [server.url]);
    await applyProfile(cdp, PROFILES[profile]);
    const marks = {};
    await page.exposeFunction("mark", (name, edge) => {
      (marks[name] ??= {})[edge] = { data: counter.snapshot(), total: totalCounter.snapshot() };
    });
    const result2 = await page.evaluate((kind2) => window.run(kind2), kind);
    await page.waitForTimeout(100);
    result2.network = counter.snapshot();
    for (const phase of result2.phases) if (marks[phase.name]) {
      phase.network = subtract(marks[phase.name].end.data, marks[phase.name].start.data);
      phase.totalNetwork = subtract(marks[phase.name].end.total, marks[phase.name].start.total);
    }
    result2.totalNetwork = totalCounter.snapshot();
    result2.totalRequests = totalCounter.entries();
    result2.requests = counter.entries();
    result2.rep = rep;
    result2.profile = profile;
    runs.push(result2);
    await context.close();
    console.log(profile, kind, rep, result2.phases[0].ms.toFixed(1));
  }
  const reference = /* @__PURE__ */ new Map();
  for (const run of runs) for (const hash of run.hashes) {
    const key = hash.t, value = JSON.stringify({ data: hash.data, mask: hash.mask, pixels: hash.pixels });
    if (reference.has(key) && reference.get(key) !== value) throw Error("Numeric/rendered hashes differ for date " + key);
    reference.set(key, value);
  }
  const result = { createdAt: (/* @__PURE__ */ new Date()).toISOString(), provenance: { commit: execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(), browser: browser.version(), node: process.version, dependencies, inputHashes, scriptHashes }, workload: { height: 905, width: 741, bands: 4, dtype: "uint16", dates: 3, level: 0, viewport: "entire raster", rendering: "shared js/demo Renderer; level0 741x905 canvas; true color inputs2,1,0 divide10000; finish sync", cachePolicy: "fresh context; HTTP cache disabled; shared frame cache for warm operations", server: "localhost; OS caches reused" }, validation: "all data, masks and rendered RGBA hashes equal across runs, outside timing", runs };
  await writeFile(new URL("./results.json", import.meta.url), JSON.stringify(result, null, 2) + "\n");
  const median = (values) => {
    values.sort((a, b) => a - b);
    const n = values.length;
    return n % 2 ? values[n >> 1] : (values[n / 2 - 1] + values[n / 2]) / 2;
  };
  const summary = {};
  for (const profile of ["natural", "50Mbit-40ms"]) for (const kind of ["chronozarr", "zarr", "cog"]) {
    const selected = runs.filter((run) => run.kind === kind && run.profile === profile);
    const phaseMedian = (prefix) => median(selected.flatMap((run) => run.phases.filter((phase) => phase.name.startsWith(prefix)).map((phase) => phase.ms)));
    summary[profile + "/" + kind] = { coldMs: phaseMedian("cold"), uncachedStepMs: phaseMedian("uncached"), nativeRepeatMs: phaseMedian("native"), warmMs: phaseMedian("warm"), renderMs: median(selected.flatMap((r) => r.phases.filter((p) => p.renderUploadFinishMs !== void 0).map((p) => p.renderUploadFinishMs))), coldRequests: selected[0].phases[0].network.requests, coldWireBytes: selected[0].phases[0].network.bytes };
  }
  await writeFile(new URL("./summary.json", import.meta.url), JSON.stringify(summary, null, 2) + "\n");
} finally {
  await browser.close();
  await server.close();
}
