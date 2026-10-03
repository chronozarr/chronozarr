import { createHash } from "node:crypto";
import { build } from "esbuild";
import { mkdir, writeFile, readFile } from "node:fs/promises";
import { startServer, launchBrowser, newPage, attachNet, subtract } from "../lib/harness.mjs";
await mkdir(new URL("./dist", import.meta.url), { recursive: true });
await build({ entryPoints: [new URL("./page.js", import.meta.url).pathname], bundle: true, external: ["/js/*"], format: "esm", target: "es2022", outfile: new URL("./dist/page.js", import.meta.url).pathname });
const inputFiles = ["input-full/source.json", "input-full/observations.csv", "input-full/2020-05-05.tif", "input-full/2020-06-09.tif", "input-full/2020-07-29.tif", "series/zarr.json", "plain-zarr/zarr.json"];
const inputHashes = {};
for (const file of inputFiles) {
  inputHashes[file] = createHash("sha256").update(await readFile(new URL("../../data/adoption/lake-mead-2020/" + file, import.meta.url))).digest("hex");
}
const dependencies = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
const server = await startServer(), browser = await launchBrowser(), runs = [];
try {
  for (let rep = 0; rep < 5; rep++) for (let i = 0; i < 3; i++) {
    const kind = ["chronozarr", "zarr", "cog"][(i + rep) % 3];
    const { context, page } = await newPage(browser);
    await page.goto(server.url + "/bench/adoption/page.html");
    await page.waitForFunction(() => window.run);
    const { counter } = await attachNet(context, page, [server.url + "/data/adoption/"]);
    const marks = {};
    await page.exposeFunction("mark", (name, edge) => {
      (marks[name] ??= {})[edge] = counter.snapshot();
    });
    const result2 = await page.evaluate((kind2) => window.run(kind2), kind);
    await page.waitForTimeout(100);
    result2.network = counter.snapshot();
    for (const phase of result2.phases) if (marks[phase.name]) phase.network = subtract(marks[phase.name].end, marks[phase.name].start);
    result2.requests = counter.entries();
    result2.rep = rep;
    runs.push(result2);
    await context.close();
    console.log(kind, rep, result2.phases[0].ms.toFixed(1));
  }
  const reference = runs[0].hashes;
  for (const r of runs) if (JSON.stringify(r.hashes) !== JSON.stringify(reference)) throw Error("Full data/mask hashes differ");
  const result = { createdAt: new Date().toISOString(), provenance: { browser: browser.version(), node: process.version, dependencies, inputHashes }, workload: { height: 905, width: 741, bands: 4, dtype: "uint16", dates: 3, level: 0, viewport: "entire raster", rendering: "none: decoded raster assembly only", cachePolicy: "fresh context; HTTP cache disabled; shared frame cache for warm operations", server: "localhost; OS caches reused" }, validation: "all data and mask full-frame SHA256 equal across 15 runs, outside timing", runs };
  await writeFile(new URL("./results.json", import.meta.url), JSON.stringify(result, null, 2) + "\n");
  const median = (values) => {
    const sorted = [...values].sort((a, b) => a - b);
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  };
  const summary = {};
  for (const kind of ["chronozarr", "zarr", "cog"]) {
    const selected = runs.filter((run) => run.kind === kind);
    const phaseMedian = (prefix) => median(selected.flatMap((run) => run.phases.filter((phase) => phase.name.startsWith(prefix)).map((phase) => phase.ms)));
    summary[kind] = { coldMs: phaseMedian("cold"), uncachedStepMs: phaseMedian("uncached"), nativeRepeatMs: phaseMedian("native"), coldRequests: selected[0].phases[0].network.requests, coldWireBytes: selected[0].phases[0].network.bytes };
  }
  await writeFile(new URL("./summary.json", import.meta.url), JSON.stringify(summary, null, 2) + "\n");

} finally {
  await browser.close();
  await server.close();
}
