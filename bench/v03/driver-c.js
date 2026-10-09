// System C: one COG per date, rendered to PNG tiles by TiTiler and drawn by a MapLibre raster layer (page-c.html
// exposes `window.maplibregl`).
//
//   __driver.open({ t, view, cfg, timeoutMs })   a MapLibre map at the view, then the raster source of date t; resolves
//                                                 at the first render after which every tile of the view is loaded
//   __driver.show(t, { timeoutMs })              the source of date t is added next to the one on screen, hidden, and
//                                                 swapped in when all of its tiles are loaded (no blank or mixed frame);
//                                                 resolves when the GPU has finished the render that draws it (common.js)
//
// cfg = { cogUrls: [url of the COG of each timestep], titilerUrl, tileQuery, bounds: [w, s, e, n], latitude, maxzoom }. The source gets its
// bounds so MapLibre requests no tile outside the footprint (what a TileJSON would tell it), and one tile URL template per date.
// The clock starts at the call that adds the first source, like system A's loadStore() and system B's addLayer().

(() => {
  let map = null;
  let cfg = null;
  let current = null;
  let sequence = 0;
  let errors = 0;

  const tiles = (t) => [`${cfg.titilerUrl}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=${encodeURIComponent(cfg.cogUrls[t])}&${cfg.tileQuery}`];

  /** The tile manager of a source (MapLibre 6 keeps them on the style); the in-view tiles are its internal state, so check the shape and say what changed. */
  const inView = (id) => {
    const manager = map.style.tileManagers?.[id];
    if (!manager || typeof manager._inViewTiles?.getAllTiles !== 'function') throw new Error('MapLibre internals changed (style.tileManagers[id]._inViewTiles.getAllTiles): update bench/v03/driver-c.js');
    return manager._inViewTiles.getAllTiles();
  };

  /** True when the source has tiles in view and every one of them is loaded (an errored tile is counted by `errors`, not as loaded). */
  const sourceComplete = (id) => {
    if (!map.isSourceLoaded(id)) return false;
    const tilesInView = inView(id);
    return tilesInView.length > 0 && tilesInView.every((tile) => tile.state === 'loaded');
  };

  const mapCanvas = () => document.querySelector('canvas.maplibregl-canvas');
  const nextRender = () => new Promise((resolve) => map.once('render', () => resolve(__bench.gpuDone(mapCanvas()))));

  /** The zoom of the tiles in view and the ground size of one tile pixel in metres (512 px tiles on the 256 px grid of WebMercatorQuad). */
  const tileInfo = (id) => {
    const tileZoom = inView(id)[0]?.tileID?.canonical?.z ?? null;
    const groundMetresPerPixel = tileZoom === null ? null : (156543.03392804097 * Math.cos((cfg.latitude * Math.PI) / 180)) / 2 ** tileZoom / 2;
    return { tileZoom, groundMetresPerPixel };
  };

  const addDate = (t, visible) => {
    const id = `cog-${++sequence}`;
    map.addSource(id, { type: 'raster', tiles: tiles(t), tileSize: 256, bounds: cfg.bounds, maxzoom: cfg.maxzoom });
    map.addLayer({ id, type: 'raster', source: id, paint: { 'raster-opacity': visible ? 1 : 0, 'raster-fade-duration': 0, 'raster-resampling': 'nearest' } });
    return id;
  };

  /** Resolves when `id` is complete, checked at every render (`sync`: with the GPU finished); rejects after timeoutMs. */
  const whenComplete = (id, timeoutMs, { sync = false } = {}) =>
    new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        map.off('render', check);
        reject(new Error(`tiles of ${id} not loaded after ${timeoutMs} ms (${inView(id).filter((tile) => tile.state === 'loaded').length} of ${inView(id).length}, ${errors} errors)`));
      }, timeoutMs);
      function check() {
        if (!sourceComplete(id)) return;
        clearTimeout(timer);
        map.off('render', check);
        resolve({ at: sync ? __bench.gpuDone(mapCanvas()) : performance.now(), tiles: inView(id).length });
      }
      map.on('render', check);
      check();
    });

  window.__driver = {
    name: 'cog+titiler',

    async open({ t, view, cfg: config, timeoutMs }) {
      cfg = config;
      map = new maplibregl.Map({
        container: 'map',
        style: { version: 8, sources: {}, layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#000' } }] },
        center: view.centerLonLat,
        zoom: view.mapZoom,
        interactive: false,
        attributionControl: false,
        fadeDuration: 0,
        canvasContextAttributes: { preserveDrawingBuffer: true },
      });
      window.map = map;
      map.on('error', () => errors++);
      await new Promise((resolve, reject) => {
        map.once('load', resolve);
        map.once('error', (event) => reject(event.error ?? new Error('map error')));
      });
      const started = performance.now();
      const id = addDate(t, true);
      const done = await whenComplete(id, timeoutMs, { sync: true });
      current = id;
      const canvas = mapCanvas();
      return { ms: done.at - started, ...tileInfo(id), cells: done.tiles, canvas: { width: canvas.width, height: canvas.height }, errors };
    },

    async show(t, { timeoutMs }) {
      const at = performance.now();
      const id = addDate(t, false);
      const done = await whenComplete(id, timeoutMs);
      map.setPaintProperty(id, 'raster-opacity', 1);
      const shownAt = await nextRender();
      map.removeLayer(current);
      map.removeSource(current);
      current = id;
      return { ms: shownAt - at, ...tileInfo(id), cells: done.tiles, shownT: t, errors };
    },

    stats: () => ({ errors }),
  };
})();
