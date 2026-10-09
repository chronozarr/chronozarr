# Embedding the viewer

`https://chronozarr.org/demo/?store=<url>` opens any chronozarr store. Add `embed=1` and the same page becomes a compact viewer for an `<iframe>`. A small `postMessage` contract lets the host page move the viewer in time, read what the user clicks and follow the camera.

The host page needs nothing installed: it sends messages with `contentWindow.postMessage` and receives them with a `message` event listener.

[js/examples/embed.html](../js/examples/embed.html) is a working host page, served at `https://chronozarr.org/examples/embed.html`. Add `?store=<url>`, `?theme=light` or `?controls=0` to that URL to try the variants.

## 1. The iframe

```html
<iframe
  id="chronozarr"
  title="Ucayali River, monthly Sentinel-2"
  src="https://chronozarr.org/demo/?embed=1&store=https%3A%2F%2Fdata.chronozarr.org%2Fucayali_santa_maria_v03&origin=https%3A%2F%2Fexample.com"
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

Build the `src` with `URLSearchParams` or `encodeURIComponent`, because `store` and `origin` are URLs inside a URL. Set `origin` to the `location.origin` of your page. Use `/demo/` as the path. The path `/demo` redirects there and keeps the query string.

## 2. URL parameters

All parameters go in the iframe URL. The parameters of the full viewer also work with `embed=1`. A permalink from the full viewer therefore becomes an embed when you add `embed=1&origin=...`.

| Parameter | Value | Default | Effect |
|---|---|---|---|
| `store` | URL-encoded base URL of a chronozarr store, the directory that holds `zarr.json` | none | The store to open. Without it the viewer shows an error and sends `chronozarr:error` with code `no_store`. An embed never falls back to another store and never fetches the catalog. |
| `embed` | `1` | off | Switches on the compact layout and the `postMessage` API. Any other value is not an embed. |
| `controls` | `0` | on | Shows only the canvas and a thin readout of the time of the frame on screen. The header, wordmark, inspector and transport controls are hidden. Clicks are still reported. Needs `embed=1`. |
| `theme` | `light` | `dark` | Uses the light palette for the chrome (header, timeline, inspector). The canvas and its pixels are the same in both themes. Needs `embed=1`. |
| `origin` | URL-encoded origin of the host page | the origin of `document.referrer` | The only origin that the viewer listens to and sends to. See section 3. |
| `t` | timestep index | 0 | Initial timestep. |
| `p` | `true_color`, `false_color`, `ndvi`, `ndwi`, `water` or `band` | the first product that the store's bands allow | Initial product. |
| `b` | band name | first band | Band of the single-band product (`p=band`). |
| `z` | number above 0 | the fitted view | Initial zoom in CSS pixels per full-resolution data pixel. The same ground width then shows in a window of any size. |
| `c` | `x,y` | the store center | Initial view center in the store's projected coordinates. A store with no georeferencing uses level-0 pixels. |

The embed layout drops the catalog selector and the export button. It keeps the product buttons and the timeline. The inspector is a drawer at every width. It opens on a click, and Escape or the close button closes it.

A small "chronozarr" wordmark in the header links to the full viewer on the same store and view. The link opens in a new tab and carries no embed parameters.

Put the initial state in the URL. The viewer also keeps the current time, product and camera in the iframe's own address, so reloading the iframe restores them.

## 3. The origin rule

Messages go only between the iframe and its parent, and only for one origin.

- `origin=<origin>` in the iframe URL names that origin. It must be an `http(s)` origin such as `https://example.com` or `http://localhost:3000`, with no path and no `*`.
- Without `origin`, the viewer uses the origin of `document.referrer`. That is your page when your `Referrer-Policy` sends at least the origin, as the browser default does. With `Referrer-Policy: no-referrer` there is no referrer, so pass `origin`.
- If neither gives a valid origin, the viewer still works but the API is off. The viewer says why with a `console.warn`.
- If `origin` is present but invalid, the viewer does not fall back to the referrer. The API is off in that case too.
- The API is also off when the page is not framed.

The viewer reads a message only if `event.origin` is that origin and `event.source` is its parent window. It drops anything else without a reply, an error or any effect.

The viewer sends every message to that origin only, never to `*`. If the real parent has another origin, the browser refuses to deliver the message. It then warns "The target origin provided ... does not match the recipient window's origin" in the console of the viewer frame. That warning means `origin` is wrong.

A page opened from `file://` has the origin `null` and cannot be a host. Serve it over `http(s)`, `localhost` included.

## 4. Messages

All messages are plain JSON objects. The `type` starts with `chronozarr:`. The contract version is `v: 1`, and every message from the viewer carries it. A message to the viewer may leave `v` out, which means 1. Any other value gets a `chronozarr:error` with code `bad_message`.

The viewer ignores messages of other protocols on the same window. It also ignores the types that it sends itself, so a host that echoes messages back gets no errors. It ignores unknown fields in a `set`, so a later version can add some.

### Host to viewer

#### `chronozarr:set`

A `set` changes the view, and every field is optional. The viewer applies the fields that are present together, in this order: product and band, display range, time, camera, speed, playback.

| Field | Type | Meaning |
|---|---|---|
| `t` | integer or ISO 8601 string | A timestep index (0 to `times.length - 1`) or a date such as `"2024-03-01"` or `"2024-03-01T12:00:00Z"`. A date selects the nearest timestep. The earlier timestep wins a tie. A date outside the series selects the first or last timestep. A date-time without a zone is read as UTC. |
| `product` | string | A product id from `ready.products` that is `available`. |
| `band` | string | A band name from `ready.bands`. It chooses the band of the single-band product. Send `product: "band"` with it to show that product. |
| `range` | `null` or `[low, high]` | Display limits in physical units for a single band that is shown with a linear stretch. `low` must be below `high`. `null` returns to the range that the viewer measures from the pixels on screen. |
| `zoom` | number above 0 | CSS pixels per full-resolution data pixel, as in `z=`. The limits are the same as for the mouse: out to half of the fitted view, in to 16 canvas pixels per data pixel. |
| `center` | `{x, y}` or `{lon, lat}` | View center. `x` and `y` are in the store's coordinates, as in `c=`, or in level-0 pixels for a store with no georeferencing. `lon` and `lat` are WGS84 degrees. They need a georeferenced store whose CRS is WGS84 UTM, EPSG:3857 or EPSG:4326. When `x` and `y` are both present they win, so you can send the `center` of a `chronozarr:view` message back unchanged. |
| `playing` | boolean | Starts or stops playback. |
| `speed` | number above 0 | Playback speed in timesteps per second. The viewer snaps it to its steps: 1 to 15, 20, 24, 30, 40, 48 and 60. |

The viewer accepts or rejects a `set` as a whole. It rejects a `set` when a field is invalid or when the store cannot satisfy it. The store cannot satisfy these fields:

- a `t` past the end
- a product that the store's bands do not allow
- a band that the store does not have
- `lon` and `lat` for a store without a transform

The viewer then applies nothing and answers with `chronozarr:error` (`bad_set`), which names the field.

A `set` that arrives before the store has opened is kept. The viewer merges several of them, with later fields winning, and applies them right after `chronozarr:ready`. For the first frame, use the URL parameters instead. The viewer then paints the requested view at once.

```js
frame.postMessage({ v: 1, type: 'chronozarr:set', t: '2024-03-01' }, VIEWER);
frame.postMessage({ type: 'chronozarr:set', product: 'ndvi', zoom: 2, center: { lon: -75.02, lat: -7.64 } }, VIEWER);
frame.postMessage({ type: 'chronozarr:set', speed: 10, playing: true }, VIEWER);
```

#### `chronozarr:get`

```js
frame.postMessage({ type: 'chronozarr:get' }, VIEWER);
```

The viewer answers with `chronozarr:ready` again, and its `state` is current. Send `get` in two cases:

- Your listener was attached after the first `ready`. A cached iframe or a framework that mounts late causes this.
- You need `state.playing`.

Before the store has opened there is no answer. `ready` follows when the store opens.

### Viewer to host

#### `chronozarr:ready`

The viewer sends `ready` when it has read the store's metadata and set up the view, before it paints the first frame. You can send commands from then on.

```json
{
  "v": 1,
  "type": "chronozarr:ready",
  "store": { "url": "https://data.chronozarr.org/ucayali_santa_maria_v03", "name": "ucayali_santa_maria_v03", "crs": "EPSG:32718" },
  "times": ["2015-11-01T00:00:00Z", "2016-04-01T00:00:00Z"],
  "bands": [{ "name": "B02", "common_name": "blue", "units": "reflectance", "scale": 0.0001, "offset": 0 }],
  "products": [{ "id": "true_color", "name": "True color", "available": true }],
  "levels": [{ "lod": 0, "width": 2759, "height": 2765, "resolution": 10 }],
  "state": {
    "t": 0, "time": "2015-11-01T00:00:00Z", "product": "true_color", "band": "B02", "range": null, "zoom": 0.175,
    "center": { "x": 499445, "y": 9156055, "lon": -75.005032, "lat": -7.634983 },
    "playing": false, "speed": 4
  }
}
```

`times` and `state.time` are the ISO strings of the store. `state.range` is `null` while the viewer measures the range itself. `bands[].common_name` and `units` are `null` when the store does not say. `levels[].resolution` is the pixel size in the store's units, or `null` without georeferencing. In `state.center`, `x` and `y` are in the store's coordinates. `lon` and `lat` are `null` when the viewer cannot compute them.

#### `chronozarr:time`

The viewer sends `time` on every change of the timestep. A change comes from a `set`, a click on the timeline, the arrow keys, the chart or a step of playback. Playback sends up to 60 a second. `t` is the timestep that was asked for. The frame on screen can still be the previous one for a moment while chunks load.

```json
{ "v": 1, "type": "chronozarr:time", "t": 8, "time": "2016-11-01T00:00:00Z" }
```

#### `chronozarr:click`

The viewer sends `click` when the user clicks a pixel inside the store. It does not send it after a drag or for a click outside the raster. It sends it with `controls=0` too.

The values belong to the frame on screen. `t` and `time` are the timestep of that frame. `level` is the pyramid level that the frame is drawn from, and level 0 is full resolution. A coarser level can appear while a view is still loading, and the values are then those of the larger pixel.

```json
{
  "v": 1,
  "type": "chronozarr:click",
  "pixel": { "x": 1155, "y": 1394 },
  "lon": -75.025341,
  "lat": -7.636068,
  "t": 0,
  "time": "2015-11-01T00:00:00Z",
  "level": 0,
  "valid": true,
  "values": { "B02": 0.0491, "B03": 0.0662, "B04": 0.0558, "B08": 0.203 }
}
```

`pixel` is the column and row of the clicked data pixel at full resolution. `lon` and `lat` are the WGS84 coordinates of the center of that pixel. They are `null` when the store is not georeferenced or its CRS is not WGS84 UTM, EPSG:3857 or EPSG:4326.

`values` holds the physical value of each band, keyed by band name. The physical value is the stored value times the band's `scale`, plus its `offset`. For Sentinel-2 bands it is reflectance. `valid` is `false`, and every value is `null`, for a pixel at the store's nodata value or masked out.

#### `chronozarr:view`

The viewer sends `view` when the camera has been still for 120 ms after a pan, zoom or resize. It sends it only when the view differs from the last view sent. A `set` that moved the camera also leads to a `view`.

```json
{ "v": 1, "type": "chronozarr:view", "zoom": 2, "center": { "x": 497386.015, "y": 9155942.492, "lon": -75.0237, "lat": -7.636 } }
```

`zoom` and `center` mean the same as in `set`, so you can send a saved `view` back to restore the camera.

#### `chronozarr:error`

```json
{ "v": 1, "type": "chronozarr:error", "code": "bad_set", "message": "zoom must be a number above 0 (CSS pixels per level-0 data pixel), got \"big\"." }
```

| `code` | Cause |
|---|---|
| `bad_message` | A message of yours has an unsupported `v` or an unknown `chronozarr:` type. |
| `bad_set` | A `set` has an invalid field or one that the store cannot satisfy. Nothing was applied. |
| `no_store` | The iframe URL has no `store`. |
| `store_open_failed` | The store could not be opened. It was not found, it has no CORS or it is not a chronozarr store. `message` has the reason. |
| `chunk_load_failed` | A chunk could not be read after retries. The cell keeps its previous data and the viewer retries it. The viewer shows a toast for this error. |
| `playback_failed` | Playback paused because the loop could not be buffered. |
| `internal` | A command failed unexpectedly. The browser console has the stack. |

A wrong or missing `origin` is not reported by message, because the viewer has nobody it may tell. The console says why (section 3).

## 5. Size

The viewer reflows down to 320 px wide. Give it at least 320 px of height, and about 4:3 on a wide page. Below 700 px the product buttons give way to one select. The inspector is a drawer over the map at every width. It is up to 340 px wide, or the whole width of a narrow iframe.

```css
#chronozarr { width: 100%; aspect-ratio: 4 / 3; min-height: 320px; max-height: 80vh; border: 0; }
```

While the pointer is over the iframe, the map takes the mouse wheel for zoom and a one-finger drag on touch screens for pan. The page does not scroll from there. Leave some page around the iframe, as a map embed does.

## 6. Stores and access

The iframe runs on `chronozarr.org`, in its own origin. It does not inherit your page's login, cookies or headers. Its requests for the store carry no credentials. The store must therefore be readable by anyone who can open the page. It must send `Access-Control-Allow-Origin: *` (or `https://chronozarr.org`). A sharded store also needs byte ranges. The other requirements are in [hosting.md](hosting.md). `chronozarr doctor <store-url>` checks them.

For a private store, the host page can put a prefix-wide signed URL in `store=`. The viewer appends the query string of `store` to every request. The token must therefore authorize every object under the prefix. These work:

- a CloudFront signed URL with a custom policy and a wildcard resource
- an Azure Blob container or prefix SAS
- a token that your own gateway accepts

A signature for one object does not work, because a store is many files. S3 and GCS presigned URLs sign one object.

The token is part of the iframe URL, so anyone who can see the page can read it. Give it a short lifetime. The wordmark link carries the same `store` value to the full viewer.

Any page can embed the viewer, because `chronozarr.org` sends no `X-Frame-Options` header and no `frame-ancestors` policy. If your page sets a Content-Security-Policy, allow `frame-src https://chronozarr.org`. A `sandbox` attribute on the iframe needs `allow-scripts allow-same-origin`. Without `allow-same-origin` the viewer's origin is `null`, and your `event.origin` check fails. The dates of these checks are in [evidence.md](evidence.md#embedding).

## 7. Stability

The `v` field versions the contract. Within `v: 1` the viewer may add optional fields to its messages and optional fields to `set`. Hosts should ignore fields and message types that they do not know. A change that removes something or changes its meaning bumps `v`. The viewer then answers a message with the old `v` with `bad_message`.
