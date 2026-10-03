import { openStore } from "/js/chronozarr/decoder.js";
import * as z from "/js/vendor/zarrita/index.js";
import { fromUrl } from "geotiff";
const base = "/data/adoption/lake-mead-2020", dates = ["2020-05-05", "2020-06-09", "2020-07-29"];
const H = 905, W = 741;
async function digest(a) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", a.buffer.slice(a.byteOffset, a.byteOffset + a.byteLength)))).map((x) => x.toString(16).padStart(2, "0")).join("");
}
window.run = async (kind) => {
  await window.mark("cold-open", "start");
  const start = performance.now();
  let store, arr, mask;
  const cache = /* @__PURE__ */ new Map(), tiffs = /* @__PURE__ */ new Map();
  if (kind === "chronozarr") store = await openStore(location.origin + base + "/series");
  if (kind === "zarr") {
    const s = await z.withConsolidatedMetadata(new z.FetchStore(location.origin + base + "/plain-zarr"));
    arr = await z.open(z.root(s).resolve("0/data"), { kind: "array" });
    mask = await z.open(z.root(s).resolve("0/mask"), { kind: "array" });
  }
  async function read(t, native = false) {
    if (!native && cache.has(t)) return cache.get(t);
    let data, m;
    if (kind === "chronozarr") {
      data = new Uint16Array(4 * H * W);
      m = new Uint8Array(H * W);
      await Promise.all([0, 1, 2, 3].map(async (i) => {
        const r = i >> 1, c = i % 2;
        const [cell, cm] = await Promise.all([store.getCell(0, r, c, t), store.getMask(0, r, c, t)]);
        for (let y = 0; y < cell.height; y++) {
          m.set(cm.subarray(y * 512, y * 512 + cell.width), (r * 512 + y) * W + c * 512);
          for (let b = 0; b < 4; b++) data.set(cell.data.subarray(b * 512 * 512 + y * 512, b * 512 * 512 + y * 512 + cell.width), b * H * W + (r * 512 + y) * W + c * 512);
        }
      }));
    } else if (kind === "zarr") {
      const [d, ma] = await Promise.all([z.get(arr, [t, null, null, null]), z.get(mask, [t, null, null])]);
      data = d.data;
      m = ma.data;
    } else {
      let tif = tiffs.get(t);
      if (!tif) {
        tif = await fromUrl(base + "/input-full/" + dates[t] + ".tif", { blockSize: 65536, cacheSize: 100 });
        tiffs.set(t, tif);
      }
      const im = await tif.getImage();
      const bands = await im.readRasters({ samples: [0, 1, 2, 3], interleave: false });
      data = new Uint16Array(4 * H * W);
      bands.forEach((b, i) => data.set(b, i * H * W));
      let mi;
      for (let i = 1; i < await tif.getImageCount(); i++) {
        const candidate = await tif.getImage(i);
        if (candidate.getWidth() === W && candidate.getHeight() === H && candidate.fileDirectory.getValue("NewSubfileType") & 4) {
          mi = candidate;
          break;
        }
      }
      if (!mi) throw Error("No full-resolution internal COG mask");
      const mb = await mi.readRasters();
      m = Uint8Array.from(mb[0], (x) => x ? 1 : 0);
    }
    const frame = { data, m };
    cache.set(t, frame);
    return frame;
  }
  const phases = [];
  let p = performance.now();
  const f = await read(0);
  phases.push({ name: "cold-open", ms: performance.now() - start });
  await window.mark("cold-open", "end");
  for (const t of [1, 2]) {
    await window.mark("uncached-step-" + t, "start");
    p = performance.now();
    await read(t);
    phases.push({ name: "uncached-step-" + t, ms: performance.now() - p });
    await window.mark("uncached-step-" + t, "end");
  }
  for (const t of [0, 2, 1, 0, 1, 2]) {
    p = performance.now();
    await read(t);
    phases.push({ name: "warm-step-" + t, ms: performance.now() - p });
  }
  for (const t of [0, 2, 1]) {
    await window.mark("native-repeat-" + t, "start");
    p = performance.now();
    await read(t, true);
    phases.push({ name: "native-repeat-" + t, ms: performance.now() - p });
    await window.mark("native-repeat-" + t, "end");
  }
  const memory = { retainedFrameBytes: [...cache.values()].reduce((s, f2) => s + f2.data.byteLength + f2.m.byteLength, 0), jsHeapBytes: performance.memory?.usedJSHeapSize ?? null, chronozarrStats: store?.stats() ?? null };
  const hashes = [];
  for (const [t, fr] of cache) hashes.push({ t, data: await digest(fr.data), mask: await digest(fr.m) });
  store?.close();
  return { kind, phases, memory, hashes };
};
