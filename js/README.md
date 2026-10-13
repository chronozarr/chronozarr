# chronozarr

Browser and Node reader for [chronozarr](https://github.com/chronozarr/chronozarr) stores. The package also has a MapLibre GL JS custom layer that draws a store.

A chronozarr store is a Zarr v3 time series of rasters with a multiscale pyramid. The writer makes one object per chunk by default and can shard the chunks. A client reads one timestep of one map cell with one HTTP request. For an unsharded store that request is a plain `GET` of one chunk. For a sharded store it is one range read of a shard, once the shard index is cached.

The reader turns `(lod, row, col, t)` into a typed array. It caches the shard indexes of sharded stores, decodes in a worker pool and prefetches a window of the time axis around the current timestep. It reads spec 0.3 stores ([spec](https://github.com/chronozarr/chronozarr/blob/main/spec/CHRONOZARR.md)). The Python package `chronozarr` writes them.

The package is plain ES modules with no build step and no runtime dependency, because zarrita and numcodecs are vendored (see [Licenses](#licenses)).

The viewer loads nothing from a third-party host at runtime and uses no web font. The MapLibre demo page, `js/maplibre/index.html`, loads a pinned version of maplibre-gl from a CDN.

## Install

```bash
npm install chronozarr
```

```js
import { openStore } from 'chronozarr';
import { ChronozarrLayer } from 'chronozarr/maplibre';
```

Without a bundler, map the names in an import map. Either point them into `node_modules`:

```html
<script type="importmap">
{ "imports": {
  "chronozarr": "/node_modules/chronozarr/chronozarr/decoder.js",
  "chronozarr/maplibre": "/node_modules/chronozarr/maplibre/layer.js"
} }
</script>
```

or at a CDN:

```html
<script type="importmap">
{ "imports": {
  "chronozarr": "https://cdn.jsdelivr.net/npm/chronozarr@0.4.0/chronozarr/decoder.js",
  "chronozarr/maplibre": "https://cdn.jsdelivr.net/npm/chronozarr@0.4.0/maplibre/layer.js"
} }
</script>
```

| Import | Content |
|---|---|
| `chronozarr` | `openStore(url, options)` and the `ChronoStore` that it resolves to |
| `chronozarr/maplibre` | `ChronozarrLayer`, a MapLibre GL JS custom layer |
| `chronozarr/decode-worker` | The module worker that `openStore` starts for decoding |
| `chronozarr/geolibre` | `createChronozarrPlugin`, a GeoLibre plugin that adds a store as a MapLibre layer |

The `chronozarr` import also exports the helpers `chunkKey`, `defaultTotalBytes`, `FetchError`, `samplePixelFrom`, `scrubCost` and `windowOrder`. `import.meta.resolve('chronozarr/decode-worker')` returns the URL of the worker, for example for the `spawnWorker` option.

`openStore` finds the decode worker relative to `decoder.js`, with `new URL('./decode-worker.js', import.meta.url)`. The worker therefore loads from wherever the package files are served: `node_modules`, a static host or a CDN.

A script from a CDN is cross-origin. The reader starts it through a same-origin `blob:` URL that imports it. The CDN must send CORS headers, and jsDelivr and unpkg do. A page with a Content Security Policy needs `worker-src blob:`.

In Node, and with `{ workers: 0 }`, chunks decode on the calling thread.

## Read a store

```js
import { openStore } from 'chronozarr';

const store = await openStore('https://your-host/v03-store');
console.log(store.times.length, store.bands, store.dtype, store.crs);
// 117 [ 'B02', 'B03', 'B04', 'B08' ] uint16 EPSG:32718

const lod = store.levels.length - 1; // the coarsest pyramid level
const { data, chunkWidth, chunkHeight } = await store.getCell(lod, 0, 0, 5); // row 0, col 0, timestep 5
// data: typed array of the store's dtype, laid out [band][y][x] over the padded chunk. Do not write to it: the cache holds this array.
const stored = (band, y, x) => data[band * chunkHeight * chunkWidth + y * chunkWidth + x];
const { scale, offset } = store.attrs.bands[2]; // B04
console.log(stored(2, 100, 100) * scale + offset); // reflectance

store.close(); // aborts in-flight requests and releases the decode workers
```

`getCell` returns the exact stored values at every timestep. `store.levels[lod]` describes each pyramid level with `gridRows`, `gridCols`, `width`, `height`, `resolution` and `transform`. `store.prefetch({ lod, cells, t })` fills the caches around a timestep. `store.stats()` reports requests, bytes and cache hits.

`prefetch` takes `playing: true` to extend the window to the whole loop. It takes `masks: true` to fetch the masks alongside the chunks.

`openStore` takes these options. `chronozarr/decoder.js` documents all of them.

| Option | Default | Meaning |
|---|---|---|
| `fetch` | `globalThis.fetch` | The fetch implementation. |
| `workers` | cores minus 1, at most 8 | The number of decode workers. With 0 the calling thread decodes. |
| `totalBytes` | 1.5 GiB on machines that report 8 GB or more, else 768 MiB | The joint cap for the decoded and compressed tiers. |
| `horizonSteps` | 12 | The timesteps on either side of `t` that idle prefetch covers. |
| `idleBytes` | 64 MiB | The bytes that idle prefetch may start per idle episode. |
| `idleMs` | 3000 | The quiet time after the last scrub or playback before the viewer counts as idle. |
| `maxRequests` | 12 | The cap on concurrent requests to the store. |
| `retryDelaysMs` | `[200, 600, 1500]` | The delay before each retry of a failed request. |

The host must send CORS headers. A sharded store also needs byte-range requests, and an unsharded store needs only `GET`. `chronozarr doctor <url>` from the Python package checks the host. The [hosting requirements](https://github.com/chronozarr/chronozarr/blob/main/docs/hosting-requirements.md) has the details.

## Draw a store on a MapLibre map

```js
import * as maplibregl from 'maplibre-gl';
import { ChronozarrLayer } from 'chronozarr/maplibre';

const map = new maplibregl.Map({ container: 'map', style: 'https://demotiles.maplibre.org/style.json' });
const layer = new ChronozarrLayer({
  url: 'https://your-host/v03-store', // store root (holds zarr.json)
  product: 'true_color', // layer.products lists what the store's bands support
  t: 0, // timestep index
  prefetch: true, // fill the reader's caches around t in the background
});
layer.on('open', () => map.fitBounds(layer.bounds, { padding: 40, duration: 0 }));
layer.on('error', (event) => console.error(event.error));
map.on('load', () => map.addLayer(layer));

slider.oninput = () => layer.setTime(Number(slider.value)); // no refetch when the timestep is cached
button.onclick = () => layer.setProduct('ndvi');
map.on('click', async (event) => console.log(await layer.getValueAt(event.lngLat))); // stored values of the pixel
```

The layer needs MapLibre GL JS 5 or later. The host page loads MapLibre, which is not a dependency of this package. The tested version is in [evidence.md](https://github.com/chronozarr/chronozarr/blob/main/docs/evidence.md#javascript-dependencies).

The layer draws on the Web Mercator projection. It supports stores in UTM, EPSG:3857 and EPSG:4326. Options, events, limits and the level-of-detail rule are in [js/maplibre/README.md](https://github.com/chronozarr/chronozarr/blob/main/js/maplibre/README.md).

## Licenses

chronozarr is Apache-2.0 (`LICENSE`). The package vendors four MIT-licensed packages.

Trevor Manz wrote three of them: zarrita, @zarrita/storage and numcodecs. They are vendored unmodified apart from a header comment, and their licenses sit beside them. The header of each file records the package version and the SHA-256 of the published file.

| Path | Package | Version | License |
|---|---|---|---|
| `vendor/zarrita/` | [zarrita](https://github.com/manzt/zarrita.js) | 0.7.5 | MIT |
| `vendor/zarrita-storage/` | [@zarrita/storage](https://github.com/manzt/zarrita.js) | 0.2.0 | MIT |
| `vendor/numcodecs/` | [numcodecs](https://github.com/manzt/numcodecs.js) | 0.3.2 | MIT |
| `vendor/gifenc.esm.js` | [gifenc](https://github.com/mattdesl/gifenc) | 1.0.3 | MIT |

The viewer export uses gifenc, written by Matt DesLauriers. It ships as one file, and its license text is in the header of that file.

`vendor/numcodecs/blosc.js`, `lz4.js` and `zstd.js` embed WebAssembly builds of Blosc (with its bundled zlib and snappy), LZ4 and Zstandard. Those C libraries carry their own permissive upstream licenses, BSD-style and zlib. numcodecs ships no separate notice for them.
