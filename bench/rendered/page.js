import { Renderer } from "/js/demo/renderer.js";
const base = "/data/adoption/lake-mead-2020", dates = ["2020-05-05", "2020-06-09", "2020-07-29"];
const H = 905, W = 741;
async function digest(a) {
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", a.buffer.slice(a.byteOffset, a.byteOffset + a.byteLength)))).map((x) => x.toString(16).padStart(2, "0")).join("");
}
window.run = async (kind) => {
  await window.mark("cold-open", "start");
  const start = performance.now();
  const canvas = document.querySelector("canvas");
  canvas.width = W;
  canvas.height = H;
  const renderer = new Renderer(canvas);
  renderer.configure({ nBand: 4, chunkWidth: W, chunkHeight: H, slots: 3, hasMask: true });
  const frameUniforms = { width: W, height: H, cx: W / 2, cy: H / 2, scale: 1, shader: 0, inputs: [2, 1, 0], stretchLo: 0, display: "tone", nodata: null, unitScale: [1, 1, 1], unitDivisor: [1e4, 1e4, 1e4], unitOffset: [0, 0, 0] };
  function paint(frame, t) {
    const begun = performance.now();
    renderer.newFrame();
    let slot = renderer.slotOf(String(t));
    if (slot < 0) {
      slot = renderer.upload(String(t), { lod: 0, row: 0, col: 0, t }, frame.data);
      renderer.uploadMask(slot, frame.m);
    }
    renderer.beginPaint(frameUniforms, { clear: true });
    renderer.drawCell({ x: 0, y: 0, w: W, h: H }, { w: W, h: H }, slot, -1, slot);
    renderer.finish();
    return performance.now() - begun;
  }
  let store, arr, mask, z, fromUrl;
  if (kind === "cog") ({ fromUrl } = await import("/bench/rendered/dist/cog.js"));
  if (kind === "zarr") z = await import("/js/vendor/zarrita/index.js");
  const cache = /* @__PURE__ */ new Map(), tiffs = /* @__PURE__ */ new Map();
  if (kind === "chronozarr") store = await (await import("/js/chronozarr/decoder.js")).openStore(location.origin + base + "/series");
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
  const hashes = [];
  async function step(t, name, started = performance.now(), native = false) {
    const readStart = performance.now();
    const frame = await read(t, native);
    const assembled = performance.now();
    const renderMs = paint(frame, t);
    const totalMs = performance.now() - started;
    await window.mark(name, "end");
    phases.push({ name, t, ms: totalMs, readAssembleMs: assembled - readStart, renderUploadFinishMs: renderMs });
    const pixels = renderer.readFrame(W, H);
    hashes.push({ name, t, data: await digest(frame.data), mask: await digest(frame.m), pixels: await digest(pixels) });
  }
  await step(0, "cold-open", start);
  for (const t of [1, 2]) {
    const name = "uncached-step-" + t;
    await window.mark(name, "start");
    await step(t, name);
  }
  for (const t of [0, 2, 1]) {
    const name = "native-repeat-" + t;
    await window.mark(name, "start");
    await step(t, name, performance.now(), true);
  }
  for (const t of [0, 2, 1, 0, 1, 2]) {
    const name = "warm-step-" + t;
    await window.mark(name, "start");
    await step(t, name);
  }
  const scrub = [];
  let requested = 0, displayed = 2, commits = 0, stale = 0;
  const tasks = [];
  const sequence = [0, 2, 1, 0, 1, 2];
  const scrubStart = performance.now();
  await window.mark("scrub", "start");
  for (const t of sequence) {
    const generation = ++requested;
    tasks.push(read(t, true).then((frame) => {
      if (generation !== requested) {
        stale++;
        return;
      }
      paint(frame, t);
      displayed = t;
      commits++;
    }));
    scrub.push({ elapsedMs: performance.now() - scrubStart, requested: t, displayed, complete: true });
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  await Promise.all(tasks);
  await window.mark("scrub", "end");
  phases.push({ name: "scrub", ms: performance.now() - scrubStart, requests: sequence.length, commits, staleDiscarded: stale, finalDisplayed: displayed, observations: scrub, partialFramesByConstruction: 0 });
  const final = cache.get(displayed);
  hashes.push({ name: "scrub-final", t: displayed, data: await digest(final.data), mask: await digest(final.m), pixels: await digest(renderer.readFrame(W, H)) });
  const memory = { retainedFrameBytes: [...cache.values()].reduce((n, f) => n + f.data.byteLength + f.m.byteLength, 0), gpuTextureBytes: 3 * W * H * 9, jsHeapPointBytes: performance.memory?.usedJSHeapSize ?? null, chronozarrStats: store?.stats() ?? null };
  store?.close();
  renderer.dispose();
  return { kind, phases, memory, hashes };
};
