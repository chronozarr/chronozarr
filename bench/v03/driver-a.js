// System A: the chronozarr viewer (js/demo/index.html) opening the v0.3 store. Injected into the viewer's page after it
// loaded; the page has no catalog, so it opens nothing by itself.
//
//   __driver.open({ storeUrl, t, search, pin })   viewer.loadStore(); resolves after the first frame that shows every
//                                             visible cell at the level the viewer chose (the viewer's definition of "complete");
//                                             `pin.lod` holds the viewer to one level (which also turns its coarse-first staging off)
//   __driver.show(t, { key, timeoutMs })     the user's input (ArrowRight / ArrowLeft, or goToTime for a jump) and the
//                                             wait for the complete frame of timestep t
//   __driver.stats()                          the reader's own request and byte counters, to cross-check the CDP counts
//
// Times are performance.now() in the page. `ms` is the time from the input to the GPU having finished the first complete
// frame of t at the level the viewer wants for the view (see common.js); `firstWholeMs` is the time to the first whole
// frame of t at any level, as the viewer's paint event reports it (the viewer shows a coarser level first when the finer
// one is not in memory, never a mixture).

(() => {
  const events = [];
  const waiters = new Set();
  const viewer = () => window.chronozarr.viewer;

  const onProbe = (event) => {
    events.push(event);
    for (const waiter of [...waiters]) waiter(event);
  };

  const waitFor = (predicate, timeoutMs) =>
    new Promise((resolve) => {
      const done = (value) => {
        clearTimeout(timer);
        waiters.delete(check);
        resolve(value);
      };
      const check = (event) => {
        if (predicate(event)) done(event);
      };
      const timer = setTimeout(() => done(null), timeoutMs);
      waiters.add(check);
    });

  const press = (key) => document.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));

  window.__driver = {
    name: 'chronozarr',

    /**
     * Before the link is throttled: start the reader's decode workers and warm their codecs by opening and closing the
     * store once (js/chronozarr/pool.js keeps the shared pool alive for 30 s after its last lease is released, and the
     * viewer's loadStore leases the same pool). Without it the workers fetch their scripts and the zstd WASM during the
     * measured open, eight times over because the HTTP cache is off, over the throttled link; B and C have loaded their
     * code (bundle, MapLibre) before the link is throttled, and a real session loads it once at page load.
     */
    async prewarm({ storeUrl }) {
      const { openStore } = await import('/js/chronozarr/decoder.js');
      const store = await openStore(storeUrl);
      await new Promise((resolve) => setTimeout(resolve, 2000));
      store.close();
    },

    async open({ storeUrl, search, pin, timeoutMs }) {
      const v = viewer();
      v.probe = onProbe;
      const started = performance.now();
      const result = await Promise.race([v.loadStore(storeUrl, { viewSearch: search, ...(pin?.lod === undefined ? {} : { lod: pin.lod }) }), new Promise((_, reject) => setTimeout(() => reject(new Error(`no complete frame after ${timeoutMs} ms`)), timeoutMs))]);
      const doneAt = __bench.gpuDone(v.canvas);
      const complete = events.find((e) => e.type === 'paint' && e.complete);
      return {
        ms: doneAt - started,
        coarseMs: result.coarseMs,
        metadataMs: result.openMs,
        level: complete?.lod ?? null,
        cells: complete?.cells ?? null,
        canvas: { width: v.canvas.width, height: v.canvas.height },
        timesteps: v.store.times.length,
      };
    },

    async show(t, { key = null, timeoutMs }) {
      const v = viewer();
      const first = events.length;
      const at = performance.now();
      const complete = waitFor((e) => e.type === 'paint' && e.complete && e.t === t && e.at >= at, timeoutMs);
      if (key) press(key);
      else v.goToTime(t);
      const frame = await complete;
      const doneAt = frame ? __bench.gpuDone(v.canvas) : null;
      const paints = events.slice(first).filter((e) => e.type === 'paint' && e.t === t);
      const whole = paints[0] ?? null;
      return {
        ms: frame ? doneAt - at : null,
        firstWholeMs: whole ? whole.at - at : null,
        level: frame?.lod ?? null,
        firstWholeLevel: whole?.lod ?? null,
        paints: paints.length,
        shownT: v.t,
      };
    },

    stats() {
      const stats = viewer().store.stats();
      return { network: stats.network, cache: stats.cache };
    },
  };
})();
