# Embedding the viewer

`https://chronozarr.org/demo/?store=<url>` opens any chronozarr store. Add `embed=1` and the same page is a compact viewer for an `<iframe>`, with a small `postMessage` contract so the host page can move it in time, read what the user clicks, and follow the camera. There is nothing to install in the host page: it talks to the iframe with `contentWindow.postMessage` and listens for `message` events.

A working host page is [js/examples/embed.html](../js/examples/embed.html), served at `https://chronozarr.org/examples/embed.html` (add `?store=<url>`, `?theme=light` or `?controls=0` to try the variants).

## 1. The iframe

```html
<iframe
  id="chronozarr"
  title="Ucayali River, monthly Sentinel-2"
  src="https://chronozarr.org/demo/?embed=1&store=https%3A%2F%2Fdata.tileripper.com%2Fucayali_santa_maria%2Fchronozarr-3&origin=https%3A%2F%2Fexample.com"
  style="display:block; width:100%; height:560px; min-height:320px; border:0"
></iframe>

<script>
  const frame = document.getElementById('chronozarr').contentWindow;
  const VIEWER = 'https://chronozarr.org';

  window.addEventListener('message', (event) => {
    if (event.origin !== VIEWER || event.source !== frame) return;      // check the sender, as the viewer checks you
    const message = event.data;
    if (message.type === 'chronozarr:ready') console.log(message.times.length, 'timesteps');
    if (message.type === 'chronozarr:click') console.log(message.lon, message.lat, message.values);
  });

  // Later, from a slider, a button, a scroll position:
  frame.postMessage({ type: 'chronozarr:set', t: 12 }, VIEWER);
</script>
```

Build the `src` with `URLSearchParams` or `encodeURIComponent`: the `store` and `origin` values are URLs inside a URL. `origin` must be your page's `location.origin`. Point the iframe at `/demo/` as above: `/demo` redirects there, preserving the query string.

## 2. URL parameters

All parameters go in the iframe URL. Every parameter the full viewer understands works with `embed=1` too, so a permalink from the full viewer becomes an embed by adding `embed=1&origin=...`.

| Parameter | Value | Default | Effect |
|---|---|---|---|
| `store` | base URL of a chronozarr store (the directory that holds `zarr.json`), URL-encoded | none: the viewer shows an error and sends `chronozarr:error` (`no_store`) | The store to open. An embed never falls back to another store and never fetches the catalog. |
| `embed` | `1` | off | Compact layout, and the `postMessage` API. Any other value is not an embed. |
| `controls` | `0` | on | Only the canvas and a thin readout of the time of the frame on screen. No header, wordmark, inspector or transport controls. Clicks are still reported. Needs `embed=1`. |
| `theme` | `light` | `dark` | Light palette for the chrome (header, timeline, inspector). The canvas and the pixels it draws are the same in both themes. Needs `embed=1`. |
| `origin` | the origin of the host page, URL-encoded | the origin of `document.referrer` | The only origin the viewer listens to and sends to; see section 3. |
| `t` | timestep index | 0 | Initial timestep. |
| `p` | `true_color`, `false_color`, `ndvi`, `ndwi`, `water`, `band` | the first product the store's bands allow | Initial product. |
| `b` | band name | first band | Band of the single-band product (`p=band`). |
| `z` | number above 0 | the fitted view | Initial zoom, in CSS pixels per full-resolution data pixel (so the same ground width shows in a window of any size). |
| `c` | `x,y` | the store center | Initial view center, in the store's projected coordinates (level-0 pixels for a store with no georeferencing). |

The embed layout drops the catalog selector and the export button, keeps the product buttons and the timeline, and shows the inspector as a drawer that opens on a click (Escape or the close button closes it), at every width. A small "chronozarr" wordmark in the header links to the full viewer on the same store and view (`target="_blank"`, without the embed parameters). Initial state belongs in the URL: the viewer also keeps the current time, product and camera in the iframe's own address, so a reload of the iframe restores them.

## 3. The origin rule

Messages go only between the iframe and its parent, and only for one origin:

- `origin=<origin>` in the iframe URL names it. It must be exactly an `http(s)` origin such as `https://example.com` or `http://localhost:3000`: no path, no `*`.
- Without `origin`, the viewer uses the origin of `document.referrer`, which is your page when your `Referrer-Policy` sends at least the origin (the browser default does). With `Referrer-Policy: no-referrer` there is no referrer, so pass `origin`.
- If neither gives a valid origin, or `origin` is present but invalid (the viewer does not then fall back to the referrer), the viewer still works but the API is off, and it says why with a `console.warn`.
- The API is also off when the page is not framed.

The viewer reads a message only if `event.origin` is that origin **and** `event.source` is its parent window. Anything else is dropped without a reply, an error, or any effect. It sends every message to that origin only, never to `*`; if the real parent is another origin, the browser refuses to deliver and warns "The target origin provided ... does not match the recipient window's origin" in the console of the viewer frame, which is the symptom of a wrong `origin`. A page opened from `file://` has the origin `null` and cannot be a host: serve it over `http(s)`, `localhost` included.

## 4. Messages

All messages are plain JSON objects with a `type` that starts with `chronozarr:`. The contract version is `v: 1`. Every message the viewer sends carries `v: 1`. A message to the viewer may leave `v` out (meaning 1); any other value is answered with `chronozarr:error` (`bad_message`). The viewer ignores messages whose `type` is not its own (another protocol on the same window) and the types it sends itself (a host that echoes messages back gets no errors). It also ignores unknown fields in a `set`, so a later version can add some.

### Host to viewer

#### `chronozarr:set`

Change the view. Every field is optional; the ones present are applied together, in this order: product and band, time, camera, speed, playback.

| Field | Type | Meaning |
|---|---|---|
| `t` | integer, or ISO 8601 string | A timestep index (0 to `times.length - 1`), or a date such as `"2024-03-01"` or `"2024-03-01T12:00:00Z"`: the nearest timestep wins (the earlier one on a tie, the first or last one outside the series). A date-time without a zone is read as UTC. |
| `product` | string | A product id from `ready.products` that is `available`. |
| `band` | string | A band name from `ready.bands`. Chooses the band of the single-band product: send `product: "band"` with it to show it. |
| `zoom` | number above 0 | CSS pixels per full-resolution data pixel, as `z=`. Limited like the mouse: out to half of the fitted view, in to 16 canvas pixels per data pixel. |
| `center` | `{x, y}` or `{lon, lat}` | View center: `x`, `y` in the store's coordinates (as `c=`; level-0 pixels for a store with no georeferencing), or `lon`, `lat` in WGS84 degrees (needs a georeferenced store whose CRS is WGS84 UTM, EPSG:3857 or EPSG:4326). When `x` and `y` are both present they win, so the `center` of a `chronozarr:view` message can be sent back as it is. |
| `playing` | boolean | Start or stop playback. |
| `speed` | number above 0 | Playback speed in timesteps per second, snapped to the viewer's steps: 1 to 15, 20, 24, 30, 40, 48, 60. |

A `set` is accepted or rejected as a whole. If a field is invalid, or the store cannot satisfy it (a `t` past the end, a product the store's bands do not allow, a band it does not have, `lon`/`lat` for a store without a transform), nothing is applied and the viewer answers with `chronozarr:error` (`bad_set`) naming the field.

A `set` that arrives before the store has opened is kept (several are merged, later fields win) and applied right after `chronozarr:ready`. For the first frame, prefer the URL parameters: with them the viewer paints the requested view at once.

```js
frame.postMessage({ v: 1, type: 'chronozarr:set', t: '2024-03-01' }, VIEWER);
frame.postMessage({ type: 'chronozarr:set', product: 'ndvi', zoom: 2, center: { lon: -73.6, lat: -9.4 } }, VIEWER);
frame.postMessage({ type: 'chronozarr:set', speed: 10, playing: true }, VIEWER);
```

#### `chronozarr:get`

```js
frame.postMessage({ type: 'chronozarr:get' }, VIEWER);
```

The viewer answers with `chronozarr:ready` again, whose `state` is current. Use it when your listener was attached after the first `ready` (a cached iframe, a framework that mounts late) and to read `state.playing`. Before the store has opened there is no answer; `ready` follows when it does.

### Viewer to host

#### `chronozarr:ready`

Sent when the store's metadata has been read and the view is set up, before the first frame is painted. Commands can be sent from here on.

```json
{
  "v": 1,
  "type": "chronozarr:ready",
  "store": { "url": "https://data.tileripper.com/ucayali_santa_maria/chronozarr-3", "name": "chronozarr-3", "crs": "EPSG:32618" },
  "times": ["2015-11-01T00:00:00Z", "2015-12-01T00:00:00Z"],
  "bands": [{ "name": "B02", "common_name": "blue", "units": "reflectance", "scale": 0.0001, "offset": 0 }],
  "products": [{ "id": "true_color", "name": "True color", "available": true }],
  "levels": [{ "lod": 0, "width": 2759, "height": 2765, "resolution": 10 }],
  "state": {
    "t": 0, "time": "2015-11-01T00:00:00Z", "product": "true_color", "band": "B02", "zoom": 0.28,
    "center": { "x": 653100, "y": 9013200, "lon": -73.6, "lat": -9.4 },
    "playing": false, "speed": 4
  }
}
```

`times` and `state.time` are the ISO strings of the store. `bands[].common_name` and `units` are `null` when the store does not say. `levels[].resolution` is the pixel size in the store's units (`null` without georeferencing). In `state.center`, `x` and `y` are in the store's coordinates and `lon`, `lat` are `null` when they cannot be computed.

#### `chronozarr:time`

Sent on every change of the timestep: a `set`, a click on the timeline, the arrow keys, the chart, and each step of playback (up to 60 a second). `t` is the timestep asked for; the frame on screen may still be the previous one for a moment while chunks load.

```json
{ "v": 1, "type": "chronozarr:time", "t": 12, "time": "2016-11-01T00:00:00Z" }
```

#### `chronozarr:click`

Sent when the user clicks a pixel inside the store (not after a drag, not outside the raster), also with `controls=0`. The values are those of the frame on screen: `t` and `time` are its timestep, which is what the numbers belong to, and `level` is the pyramid level it is drawn from (0 is full resolution; a coarser level while a view is still loading, so the values are then those of the larger pixel).

```json
{
  "v": 1,
  "type": "chronozarr:click",
  "pixel": { "x": 1204, "y": 311 },
  "lon": -73.61204,
  "lat": -9.38117,
  "t": 12,
  "time": "2016-11-01T00:00:00Z",
  "level": 0,
  "valid": true,
  "values": { "B02": 0.0412, "B03": 0.0633, "B04": 0.0587, "B08": 0.2841 }
}
```

`pixel` is the column and row of the clicked data pixel at full resolution. `lon` and `lat` are the WGS84 coordinates of that pixel's center, `null` when the store is not georeferenced or its CRS is not WGS84 UTM, EPSG:3857 or EPSG:4326. `values` holds the physical value of each band (stored value times the band's `scale`, plus its `offset`; reflectance for Sentinel-2 bands), keyed by band name. `valid` is `false`, and every value `null`, for a pixel at the store's nodata value or masked out.

#### `chronozarr:view`

Sent when the camera has been still for 120 ms after a pan, zoom or resize, and only when it differs from the last view sent. Also follows a `set` that moved the camera.

```json
{ "v": 1, "type": "chronozarr:view", "zoom": 2, "center": { "x": 653100, "y": 9013200, "lon": -73.6, "lat": -9.4 } }
```

`zoom` and `center` mean what they mean in `set`, so a saved `view` can be sent back to restore the camera.

#### `chronozarr:error`

```json
{ "v": 1, "type": "chronozarr:error", "code": "bad_set", "message": "zoom must be a number above 0 (CSS pixels per level-0 data pixel), got \"big\"." }
```

| `code` | Cause |
|---|---|
| `bad_message` | A message of yours has an unsupported `v` or an unknown `chronozarr:` type. |
| `bad_set` | A `set` with an invalid field, or one the store cannot satisfy. Nothing was applied. |
| `no_store` | The iframe URL has no `store`. |
| `store_open_failed` | The store could not be opened (not found, no CORS, not a chronozarr store). `message` has the reason. |
| `chunk_load_failed` | A chunk could not be read after retries. The cell keeps its previous data and is retried; this is the toast the viewer shows. |
| `playback_failed` | Playback paused because the loop could not be buffered. |
| `internal` | A command failed unexpectedly. The browser console has the stack. |

A wrong or missing `origin` is not reported by message, since there is nobody the viewer may tell; its console says why (section 3).

## 5. Size

The viewer reflows down to 320 px wide. Give it at least 320 px of height, and about 4:3 on a wide page. Below 700 px the product buttons give way to one select. The inspector is a drawer over the map at every width, up to 340 px wide (the whole width on a narrow iframe).

```css
#chronozarr { width: 100%; aspect-ratio: 4 / 3; min-height: 320px; max-height: 80vh; border: 0; }
```

The map takes the mouse wheel (zoom) and one-finger drag on touch screens (pan) while the pointer is over the iframe, so the page does not scroll from there. Leave some page around the iframe, as a map embed does.

## 6. Stores and access

The iframe runs on `chronozarr.org`, in its own origin. It does not inherit your page's login, cookies or headers, and its requests for the store carry no credentials. The store must therefore be readable by anyone who can open the page: publicly readable, `Access-Control-Allow-Origin: *` (or `https://chronozarr.org`), byte ranges and the other requirements of [hosting.md](hosting.md); `chronozarr doctor <store-url>` checks them.

For a private store the host can put a prefix-wide signed URL in `store=`. The viewer appends the query string of `store` to every request it makes, so the token must authorize every object under the prefix: a CloudFront signed URL with a custom policy and a wildcard resource, an Azure Blob container or prefix SAS, or a token your own gateway accepts. A signature for one object (an S3 or GCS presigned URL) does not work, since a store is many files. The token is part of the iframe URL, so anyone who can see the page can read it; give it a short lifetime, and note that the wordmark link carries the same `store` value to the full viewer.

`chronozarr.org` sends no `X-Frame-Options` and no `frame-ancestors` restriction: any page may embed it. If your page sets a Content-Security-Policy, allow `frame-src https://chronozarr.org`. A `sandbox` attribute on the iframe needs `allow-scripts allow-same-origin` (without the second the viewer's origin is `null` and your `event.origin` check fails); other sandbox settings are untested.

## 7. Stability

The contract is versioned by `v`. Within `v: 1` the viewer may add optional fields to its messages and optional fields to `set`; hosts should ignore fields and message types they do not know. A change that removes or changes the meaning of something bumps `v`, and the viewer then answers a message with the old `v` with `bad_message`.
