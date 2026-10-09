# chronozarr on MapLibre

`ChronozarrLayer` draws a chronozarr store on a MapLibre GL JS map as a custom layer. It opens the store with `js/chronozarr/decoder.js` and picks the pyramid level from the map zoom. It uploads the raw chunks of the visible cells as integer or float textures. It runs the product band math in the fragment shader.

Each cell is a small mesh. Its vertices were projected from the store's CRS to Web Mercator, so the raster lands on the basemap with no resampling step. The layer has no dependencies: MapLibre belongs to the host page.

## Demo

`js/maplibre/index.html` shows the live Ucayali store over MapLibre's demo tiles. It has a time slider, product buttons, opacity and a click readout.

To run it, serve the repository root and open `/js/maplibre/index.html`, for example with `uv run python -m http.server 8000`. The page loads MapLibre GL JS and its stylesheet from cdn.jsdelivr.net through an import map. This is the only external script.

The layer reads `defaultProjectionData.mainMatrix`, a float64 mercator-to-clip matrix. MapLibre passes it from version 5 on. The layer emits an `error` if the matrix is missing. The tested version is in [evidence.md](../../docs/evidence.md#javascript-dependencies).

## Integration

```js
import * as maplibregl from 'maplibre-gl';
import { ChronozarrLayer } from './js/maplibre/layer.js';

const map = new maplibregl.Map({ container: 'map', style: 'https://demotiles.maplibre.org/style.json' });
const layer = new ChronozarrLayer({
  url: 'https://your-host/v03-store', // store root (holds zarr.json)
  product: 'true_color', // see layer.products; 'band' shows one band
  t: 0, // timestep index
  prefetch: true, // fill the reader's caches around t in the background (default false)
});
layer.on('open', () => map.fitBounds(layer.bounds, { padding: 40, duration: 0 }));
layer.on('error', (event) => console.error(event.error));
map.on('load', () => map.addLayer(layer)); // optional second argument: the layer to draw below

slider.oninput = () => layer.setTime(Number(slider.value)); // no refetch when the timestep is cached
button.onclick = () => layer.setProduct('ndvi');
map.on('click', async (e) => console.log(await layer.getValueAt(e.lngLat))); // stored values of the pixel
// layer.setOpacity(0.6); layer.remove() frees the GPU objects, workers and in-flight requests.
```

## API

### Options

`new ChronozarrLayer(options)` takes these options.

| Option | Default | Meaning |
|---|---|---|
| `url` | required | The root URL of the store, the directory that holds `zarr.json`. |
| `id` | `'chronozarr'` | The MapLibre layer id. |
| `product` | `'true_color'` | The product id. `layer.products` lists the ids. |
| `band` | 0 | The band index or name for the `'band'` product. |
| `t` | 0 | The timestep index. |
| `opacity` | 1 | The opacity, from 0 to 1. |
| `prefetch` | `false` | Fills the reader's caches around `t` in the background. |
| `meshDivisions` | 8 | The quads per cell side in the warp mesh. |
| `gpuBudgetBytes` | 256 MiB | The size of the GPU texture pool. |
| `lodBias` | 0.5 | The bias of the pyramid level choice. Negative values pick finer levels. See [Level of detail](#level-of-detail). |
| `stretchLo` | measured once from the first complete view | The shadow-lift stretch of reflectance products. |
| `range` | measured per band from the first complete view | `[min, max]` in physical units for single-band products that use a linear stretch. |
| `storeOptions` | `{}` | The options that the layer passes to `openStore`: `fetch`, `workers`, `decodedBytes` and others. |

### Methods

| Method | Description |
|---|---|
| `setTime(t)` | Shows timestep `t`, an integer index into `layer.times`. Throws `RangeError` outside the axis. |
| `setProduct(id, band?)` | Shows `true_color`, `false_color`, `ndvi`, `ndwi`, `water` or `band`. Once the store has opened, it throws `RangeError` when the store lacks the bands. |
| `setOpacity(o)` | Sets the opacity from 0 to 1. |
| `getValueAt(lngLat, { t }?)` | Returns a promise of the stored values at a map position, or of `null` outside the footprint. The values come from the finest level. See below. |
| `on(type, fn)` | Adds a listener and returns a function that removes it. |
| `off(type, fn)` | Removes a listener. |
| `remove()` | Removes the layer from the map. It releases GPU buffers, textures and the program, aborts in-flight requests and releases the decode workers. |

`getValueAt` takes `lngLat` as `{lng, lat}` or `[lng, lat]`. The optional `t` defaults to the current timestep. The layer fetches the level-0 chunks if they are not cached. The promise resolves to an object with these fields:

- `t` and `time` are the timestep and its ISO time.
- `col` and `row` are the pixel of level 0.
- `lngLat` is the center of that pixel, and `x` and `y` are the same point in the CRS of the store.
- `valid` follows the mask when the store has one, and the nodata value otherwise.
- `bands` has one entry per band with `name`, `units`, `reflectance`, `stored` and `value`. `stored` is the stored number. `value` is the stored number times `scale`, plus `offset`.
- `ndvi`, `ndwi` and `isWater` are the products that the bands allow.

A removed layer cannot be added again. The reader ends an idle worker pool after 30 s.

### Events

| Event | Meaning |
|---|---|
| `open` | The store metadata is read. `layer.times`, `layer.bounds` and `layer.products` are valid. |
| `loading` | The view needs data. |
| `ready` | Everything that the view needs is on the GPU. It fires again after each pan, zoom or time change that had to wait. |
| `error` | Carries `event.error`. The layer keeps running. |

### State

These members are read-only: `opened`, `store`, `times`, `bounds`, `bandNames`, `products`, `t`, `product`, `opacity`, `lod` and `stats`. `opened` is a promise of the open store. `stats` counts GPU slots, uploads, evictions, meshes and pending loads.

### Caching and prefetch

A time change never refetches what the reader has cached. The GPU pool keeps recently shown chunks, and the reader cache (`layer.store`) holds decoded chunks. While a new timestep loads, the previous one stays on screen.

With `prefetch: true` the reader also fills its caches in the background through `store.prefetch`. It fetches the nearest timesteps first, within its cache and speculative-bandwidth budgets. Scrubbing a whole time series then finds most timesteps cached. Prefetch can move hundreds of MB, and [evidence.md](../../docs/evidence.md#maplibre-layer) has a measured example.

### Stored data and colors

The layer draws any spec v0.3 store. The data types are `uint8`, `uint16`, `int16` and `float32`. Each band has a `scale` and an `offset`. Validity comes from `nodata` or from a `mask`.

The product colors are `PRODUCT_GLSL` from `js/shared/products-glsl.js`, the same shader code as the viewer. `displayMode` in `js/shared/products.js` decides how a product is shown:

- An 8-bit RGB store, with scale 1 and offset 0, is shown as stored.
- A single band gets a linear stretch unless it looks like reflectance. A band looks like reflectance when the largest value of its dtype is 10 or less after scale and offset. The stretch runs from the 2nd to the 98th percentile of the valid values on screen, or over `range` in physical units.
- Every other product keeps the tone-mapped reflectance look.

## Limits

### Projections

Each store has one projection. The layer supports WGS84 UTM (EPSG:326zz and EPSG:327zz), EPSG:3857 and EPSG:4326. For any other CRS it throws an error that names the supported set. A store that crosses the antimeridian or a UTM zone boundary is not supported. The store must declare its level-0 `transform`.

The layer draws on Mercator maps only. In globe projection it draws nothing and emits an `error` once.

### Level of detail

The layer chooses the pyramid level with this rule:

```
level = clamp(floor(log2(1 / p) + lodBias), 0, levels - 1)
p = mercatorPerTexel0 * 512 * 2^zoom
```

`p` is the size of a level-0 texel in CSS pixels at the center of the store. It includes the UTM scale and the Mercator stretch.

- `lodBias` 0.5 picks the level whose texels are closest to one CSS pixel.
- `lodBias` 0 is the reader rule of the spec: the largest level whose texels are no bigger than a pixel.
- Negative values pick finer levels. They look crisper on high-density screens and load more data.

The level comes from the zoom at the map center. A pitched view therefore undersamples its far half and loads more cells. Zooming in past level 0 magnifies the texels. The layer interpolates nothing, so values are exact and edges are square.

The layer pads coarse levels to whole texels. It draws only the part inside the level-0 footprint, so the outline does not move when the level changes.

### Memory

`gpuBudgetBytes` sets the texture pool. The pool is made of slots, and each slot holds one chunk: `n_band * chunk^2 * bytes`. A slot is 2 MiB for 4 bands of uint16 at 512 px, so the default budget gives 128 slots. A cell on screen needs one data slot. A store with a mask also needs one mask slot per chunk.

A view draws at most half of the slots as cells, which is 64 at the default budget. If the view needs more cells, the layer draws the cells nearest the center and emits an `error`. The layer uploads at most 16 MiB per frame.

The CPU caches of the reader are separate. `storeOptions.totalBytes` sets their cap, and the default is in [js/README.md](../README.md#read-a-store). The decoded and compressed tiers share the cap. These caches make a warm time change a zero-request operation, and `prefetch` fills them within those budgets.

### Placement

Vertex positions are float32 offsets from the center of each mesh, and the matrix is composed in float64. Adjacent cells share their edge vertices, so the raster has no seams. The error bounds and the alignment results are in [evidence.md](../../docs/evidence.md#maplibre-layer).

Each cell is a mesh of 8 x 8 quads (`meshDivisions`). A store with much larger cells, kilometers per pixel, needs more divisions.

### Loading order

While a cell loads, the layer paints the last timestep shown there. If there is none, it paints the same area from a coarser level that is already loaded. The first view fetches the cells of the coarsest level first. It fetches the level that the zoom asks for after them. A measured example is in [evidence.md](../../docs/evidence.md#maplibre-layer).

## Checking alignment

`verify/` holds the check that compares the placement with pyproj. It does not use MapLibre's own tiles, because the demo tiles have no linework near the Ucayali store. `truth.py` writes the pyproj positions of the store outline and of the texel boundaries. `align.js` runs in the demo page. It compares those positions with what the GPU drew, using screenshots taken with the layer at opacity 1 and 0. The results are in [evidence.md](../../docs/evidence.md#maplibre-layer).

To run the check:

1. Write the pyproj positions for the Ucayali store.

   ```bash
   uv run --with pyproj python3 js/maplibre/verify/truth.py --epsg 32718 --x0 485650 --y0 9169880 --res 10 --width 2759 --height 2765 > truth.json
   ```

2. Open the demo page and hide its panel.
3. Add `verify/align.js` to the page as a script.
4. Move the map to a view with `jumpTo`.
5. Take a screenshot with `layer.setOpacity(1)`.
6. Take a second screenshot with `layer.setOpacity(0)`.
7. Pass both screenshots and `truth.outline` to `__align.mask`.
