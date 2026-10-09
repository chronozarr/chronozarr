// System B: CarbonPlan zarr-layer on MapLibre, reading the same v0.3 store unmodified (bench/zarr-layer/page.html
// exposes `window.zl`). zarr-layer is given only `zarrVersion: 3`, which skips its probes for Zarr v2 files; it finds
// the projection, bounds and levels in the store's own metadata.
//
//   __driver.open({ storeUrl, t, view, timeoutMs })   a MapLibre map at the view, then the layer; resolves when every
//                                                      visible region of the level zarr-layer chose holds timestep t
//   __driver.show(t, { timeoutMs })                    layer.setSelector() for timestep t and the wait for the same condition
//
// The clock starts at the call that adds the layer (the map exists before), like system A's loadStore() and system C's
// first addSource(), and stops when the GPU has finished the frame that shows the condition (see common.js).

(() => {
  let layer = null;
  const selector = (t) => ({ band: ['B04', 'B03', 'B02'], time: { selected: t, type: 'index' } });
  const mapCanvas = () => document.querySelector('canvas.maplibregl-canvas');

  /** Resolves at the first MapLibre render after which every visible region holds selector version >= `version`, with the layer's state then. */
  const whenShown = (version, timeoutMs) =>
    new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        window.map.off('render', check);
        reject(new Error(`layer not complete after ${timeoutMs} ms: ${JSON.stringify(zl.layerState(layer))}`));
      }, timeoutMs);
      function check() {
        const state = zl.layerState(layer);
        if (state.total > 0 && state.loaded === state.total && state.version >= version) {
          clearTimeout(timer);
          window.map.off('render', check);
          resolve({ at: __bench.gpuDone(mapCanvas()), ...state });
        }
      }
      window.map.on('render', check);
      check();
    });

  window.__driver = {
    name: 'zarr-layer',

    async open({ storeUrl, t, view, timeoutMs }) {
      await zl.createMap({ center: view.centerLonLat, zoom: view.mapZoom });
      const started = performance.now();
      layer = await zl.addLayer({ source: storeUrl, extra: { zarrVersion: 3 }, selector: selector(t) });
      const state = await whenShown(0, timeoutMs);
      const canvas = mapCanvas();
      return { ms: state.at - started, level: state.level, cells: state.total, canvas: { width: canvas.width, height: canvas.height } };
    },

    async show(t, { timeoutMs }) {
      const at = performance.now();
      await layer.setSelector(selector(t));
      const version = layer.regionRenderer.selectorVersion;
      const state = await whenShown(version, timeoutMs);
      return { ms: state.at - at, level: state.level, shownT: t };
    },

    stats: () => null,
  };
})();
