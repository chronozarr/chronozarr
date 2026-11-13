# Explore the viewer

**Package version — 0.5.2.** This guide describes the viewer included in the npm package and built from the same source for chronozarr.org. See [self-host the viewer](viewer-distribution.md) to copy it to your own host.

[The live demo](https://chronozarr.org/demo/) loads 117 monthly Sentinel-2 composites of the Ucayali River in Peru, November 2015 to February 2026, from a public storage bucket. Package publication and deployment of the hosted viewer are separate steps. Installing the Python package does not update the hosted demo.

## Where it starts

The viewer opened without a selected store or view starts its catalog's default store at the latest well-covered timestep. Among the newest 24 timesteps, that is the latest in which at least half of the pixels are observed, according to the store's coverage plane, or valid, according to its mask. A store with neither plane starts at its last timestep. The public Ucayali store has neither, so it starts at its last month. A copied viewer has an empty catalog until you supply a store.

A link with a timestep (`t`) opens at that timestep. If it specifies a store, product, zoom or centre but omits `t`, it opens at the first timestep. Preview, share and publish links follow the same rule.

## Controls

| To | Do this |
|---|---|
| Play or pause | Click the play button, or press Space |
| Step one timestep | Press the Left or Right arrow key, or click the arrows beside the date |
| Scrub through time | Hold the Left or Right arrow key, or drag on the timeline |
| Jump to a date | Click or drag on the timeline |
| Go to the first or last timestep | Focus the timeline, then press Home or End |
| Change the playback speed | Drag the speed slider |
| Zoom | Scroll, pinch on a touch screen, or focus the map and press plus or minus. Press 0 with the map focused to fit the whole store |
| Pan | Drag the map, or focus it and press an arrow key |
| See a pixel's values over time | Click the map, or focus it and press Enter to inspect the centre. The inspector shows a snapshot at the displayed date and a chart across time. In a narrow or embedded view, Escape closes the inspector drawer. The desktop side panel stays visible |
| Copy a pixel's location | Click the map, then Copy beside Latitude, longitude. Coordinates are WGS84 degrees at the selected native pixel's centre, latitude first |
| Change the product | Choose it in the product list, or press 1 to 9 |
| Change the display range of one band | Choose Single band. For a band with adjustable limits, set the minimum and maximum, or use Auto to fit the pixels on the screen. Bands shown as reflectance, including the quickstart sample, use fixed limits and show no range or Auto controls |
| Color one band | Choose Single band, then pick Viridis, Magma or Turbo in the colormap list. When range controls are available, the bar between the minimum and maximum shows the colors |
| Show zero coverage | Click Gaps. It hatches pixels whose coverage is 0. It needs a store with a coverage plane |
| Copy a link to this view | Click Copy link |
| Open another store | Paste its URL in the store field and click Open |
| Save the loop as a video | Click Export, choose MP4, WebM or GIF and the range, click Export, then click the Download link. Firefox records WebM and GIF only. Embedded viewers have Export too |
| Show load and decode statistics | Press D |

The date between the step buttons is the date of the image on the map. If the selected date is still loading or could not be loaded, a notice names it and the date still on screen.

Playback buffers two seconds of frames before starting. As the store loads, the viewer measures the link and counts the upcoming frames that still need to be downloaded. If the link cannot sustain your chosen speed, playback slows to match it. The label shows both speeds (`4 /s → 0.7`) with an explanation (`0.7 step/s: link too slow for 4`). Cached frames play faster, and playback returns to your chosen speed once the loop is in memory.

A drag on the timeline, a held arrow key and arrow-key presses less than 0.3 s apart are a scrub. While you scrub, the viewer draws the sharpest level whose next frames are already loaded. That level can be coarser than usual.

A scrub ends when you release the pointer or the held arrow key. Presses that you do not hold end it 0.3 s after the last press. The viewer then draws the date you stopped on at the usual level for your zoom.

At level 0, zero coverage means that no observation was recorded for that pixel. Overview coverage is a rounded mean and can become zero even when part of the block was observed. Zoom to native resolution before interpreting a hatch as an observation gap.

## Products

The viewer computes each product on your graphics card from the stored bands. A product appears in the list when the store has the bands it needs.

| Product | Bands it needs |
|---|---|
| True color | red, green, blue |
| False color | near infrared, red, green |
| NDVI | near infrared, red |
| NDWI | green, near infrared |
| Water | green, near infrared |
| Single band | any one band |
| Dual-pol composite | co-pol and cross-pol, in dB |

The Dual-pol composite draws co-pol in red, cross-pol in green and their ratio in dB in blue. The co-pol and cross-pol bands are the bands named VV and VH, or HH and HV. The viewer sets each channel's range once, from the first pixels on the screen, and keeps it while you step through time. A store with such a pair opens on this product.

The store names these roles in its band metadata. `chronozarr bands STORE` lists them and sets them. See [Band roles and the first view](python.md#band-roles-and-the-first-view).

## Links to a view

Copy link copies the address of the current view. It holds the store, the timestep, the product, the band with its display range and colormap, the zoom and the map centre. It does not hold the playback speed or the loop setting. If your browser blocks automatic copying, copy the selected URL in the dialog.

If a shared store fails to open, the viewer shows the error. Use the store field to try a corrected URL or choose Open default dataset to go to the demo.

The address uses these parameters:

| Parameter | Meaning | Example |
|---|---|---|
| `store` | URL of the store | `https://data.example.org/my_store` |
| `t` | Timestep index, from 0 | `t=71` |
| `p` | Product | `p=ndvi` |
| `b` | Band, for Single band | `b=B08` |
| `r` | Display range low,high in physical units, for Single band with adjustable limits; ignored for bands shown as reflectance | `r=0,100` |
| `cm` | Colormap for Single band: `gray`, `viridis`, `magma` or `turbo` | `cm=viridis` |
| `z` | Zoom, in screen pixels per stored pixel | `z=2.5` |
| `c` | Map centre x,y in the store's coordinate system | `c=684500,9123350` |

`chronozarr link STORE_URL` prints the same kind of link from the command line and checks it against the store. It sets `t`, `p`, `b`, `r`, `cm`, `z` and `c` with `--time`, `--product`, `--band`, `--range`, `--colormap`, `--zoom` and `--lonlat` or `--xy`. Use `--viewer` to point the link at your hosted viewer. To show the viewer inside your own page, see [Embed the viewer](embedding.md).

### Choose a date and a place

```sh
chronozarr link https://data.example.org/my_store --time 71 --lonlat -64.62,-3.41 --zoom 2
```

- `--lonlat LON,LAT` is a longitude and a latitude in degrees, longitude first. `link` converts them to the store's coordinate system and writes the result as `c`. A store declaring only `EPSG:4326` needs no projection library. Projected coordinates and stored WKT2/PROJJSON definitions need pyproj from the `geo` extra to interpret the definition or prove it equivalent to its code, including a geographic store with redundant WKT2. A lower-priority code never replaces an unverified definition. A longitude moves by 360 degrees when that puts it inside the store, so `-64.6` finds a store that counts longitudes from 0 to 360.
- `--xy X,Y` is the point in the store's own coordinate system, the CRS that `chronozarr info` prints. Use it when you already have those numbers, for example from the `c` of a copied link. It remains usable without pyproj, as does a link without a centre. Give `--lonlat` or `--xy`, not both.
- `link` refuses a point that the store does not cover and prints what it does cover. The viewer would move such a centre to the nearest edge without a message.
- `--zoom Z` is `z`, the number of screen pixels that draw one stored pixel. At 1 each stored pixel is one screen pixel, at 4 it is a square of 4 by 4, and at 0.5 one screen pixel shows 2 by 2 stored pixels. The map shows its width in screen pixels, times the pixel size, divided by the zoom: on a map 1,000 pixels wide, a store with 10 m pixels shows 10 km at zoom 1, 2.5 km at zoom 4 and 20 km at zoom 0.5. A centre without `--zoom` gets zoom 1. A zoom without a centre zooms on the middle of the store. The viewer cannot draw more than 16 device pixels per stored pixel, or less than half of what fits the whole store, and moves a zoom beyond that to the limit.

## Automating the viewer

A script that drives the viewer in a browser, such as a test, a screenshot job or an agent, can read what the viewer is doing instead of waiting a fixed time. The viewer page publishes this on `window.chronozarr.viewer`. A host page that embeds the hosted viewer in an iframe is on another origin and cannot reach that object. For it, the `chronozarr:status` message carries the same fields: see [`chronozarr:status`](embedding.md#chronozarrstatus) in the embedding guide.

### The status

`window.chronozarr.viewer.status` returns a snapshot object. It is a plain object, so a browser automation tool can return it from the page.

| Field | Values | Meaning |
|---|---|---|
| `store` | `none`, `opening`, `open`, `failed` | `none`: no store is open and none is being opened, as on a page that was given no store. `opening`: a store is being read. `open`: a store is open. `failed`: the last open failed and no store is open. |
| `frame` | `none`, `loading`, `preview`, `settled`, `unavailable` | The outcome of the last render of the view. `none`: no store is open. `loading`: the frame asked for is not on the canvas, which shows a coarser frame, another timestep or nothing yet. `preview`: a coarser frame of the timestep asked for is on the canvas, as during a fast scrub. `settled`: the frame asked for is on the canvas at the level the view settles on. `unavailable`: the frame cannot be had, because every chunk it misses failed for good. In the status, `frame` also reads `loading` from the moment a change of view queues a render until that render has run. |
| `playback` | `paused`, `starting`, `playing`, `rebuffering` | `paused`: playback is stopped, or no store is open. `starting`: playback was asked for and the viewer loads the frames of the first step. `playing`: the timestep advances at the playback speed. `rebuffering`: playback ran out of frames and waits for them. |
| `exporting` | `true`, `false` | `true` while an export to MP4, WebM or GIF runs. |
| `t` | integer | The timestep asked for. |
| `shownT` | integer or `null` | The timestep of the frame on the canvas. `null` before the first frame. |
| `level` | integer or `null` | The pyramid level of the frame on the canvas. Level 0 is full resolution. `null` before the first frame. |
| `error` | `null` or `{code, toast}` | The notice on screen, `null` when there is none. `code` is `store_open_failed`, `no_store`, `chunk_load_failed` or `playback_failed`, as in the [error codes of the embedding guide](embedding.md#chronozarrerror). `toast` is `true` for a notice that fades by itself and `false` for the error box that stays until the next open. |

### Waiting for the viewer

`await window.chronozarr.viewer.whenSettled()` resolves with the status once nothing is pending: no store is opening, and `frame` is `settled`, `unavailable` or `none`. It resolves on the next microtask when that holds already.

- A change of view made before the call is waited for, including a change of timestep, zoom, centre or product.
- A `preview` frame is not settled. The wait ends when the scrub ends and the frame at the final level is on the canvas.
- When the frame cannot be had, it resolves with `frame: 'unavailable'` after the viewer has given up on the chunks, and `error` holds the `chunk_load_failed` notice.
- When an open fails, it resolves with `store: 'failed'`, `frame: 'none'` and `error.code` `store_open_failed`. A `?store=` value that cannot be read as an address resolves with `store: 'none'` and the same error code.
- It does not wait for playback to stop, for an export to end or for a notice to fade. Read `playback`, `exporting` and `error` in the status it returns.

Before the page starts to open a store, the viewer has `store: 'none'` and `frame: 'none'`, which counts as settled, and `whenSettled()` resolves at once. A script that loads a page with `?store=` first waits for `window.chronozarr.ready`. It exists once the page has started to open the store, and its promise resolves with the result of the open. `firstPaintMs` in that result is a number when the first complete frame is drawn, and `null` when every cell of that frame failed for good. The promise resolves with nothing (`undefined`) when the open failed, and with `null` when a newer open replaced it.

### Attributes of the page

The viewer keeps three attributes on the `<html>` element, from the start of the page: `data-store`, `data-frame` and `data-playback`. `data-store` and `data-playback` hold the `store` and `playback` of the status. `data-frame` holds the outcome of the last render: unlike `frame` in the status, it does not turn to `loading` while a render is only queued. A script can wait for a value with a selector such as `html[data-frame="settled"]`.

The attributes change at the start, the success and the failure of an open, when a render ends with another frame outcome (a frame is drawn, or a view change makes it load), and when playback changes between `paused`, `starting`, `playing` and `rebuffering`. A notice and an export leave them as they are. `t` is not an attribute: a change of timestep changes `data-frame` only when the render it queues ends with another outcome, as any change of view does. A script that has changed the view therefore awaits `whenSettled()` first, and reads the attributes after it.

### Example

This Playwright script opens a store, goes to a timestep, waits for its frame and takes a screenshot. The launch arguments give the page WebGL2 on a machine without a GPU.

```js
import { chromium } from 'playwright';

const store = 'https://data.chronozarr.org/ucayali_santa_maria_v03';
const browser = await chromium.launch({
  args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'],
});
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } });
await page.goto(`https://chronozarr.org/demo/?store=${encodeURIComponent(store)}`);

// The first complete frame: ready resolves with nothing when the store could not be opened,
// and with a null firstPaintMs when every cell of the first frame failed.
await page.waitForFunction(() => window.chronozarr?.ready);
const opened = await page.evaluate(() => window.chronozarr.ready);
if (!opened || opened.firstPaintMs === null) throw new Error(`the viewer could not open ${store}`);

// Go to timestep 12 and wait until its frame is on the canvas.
const status = await page.evaluate(() => {
  const { viewer } = window.chronozarr;
  viewer.goToTime(12);
  return viewer.whenSettled();
});
console.log(status.frame, status.shownT, status.error);

await page.screenshot({ path: 'timestep-12.png' });
await browser.close();
```

`status.frame` is `settled` and `status.shownT` is 12 when the frame was drawn. When the chunks of the timestep cannot be read, `status.frame` is `unavailable` and `status.error` names the failure.

## What the browser downloads

The map is divided into cells of 512 by 512 pixels. For each view, the browser downloads the cells on the screen, at the current timestep, at the level of detail that matches the zoom. It also downloads the timesteps around the current one in the background. Stepping through time then usually needs no new download. A link to a store gives read access to the whole store, but the browser fetches only what the view needs.

## Privacy

The viewer page and its scripts come from chronozarr.org. The page runs no analytics script and sets no cookies. It keeps the playback speed that you choose in your browser's local storage. The store's data travels from the store's host to your browser, and chronozarr.org receives none of it. The page address, with its `store=` value, does reach chronozarr.org's host, as any page request does. [Private stores](private.md#1-what-a-viewer-link-exposes) explains what a viewer link exposes.

The documentation pages are separate from the viewer. They load their fonts from Google Fonts and use Cloudflare Web Analytics.

## Browser support

The viewer needs WebGL2. Current versions of Chrome, Edge, Firefox and Safari have it. A store that you preview from your own computer is served from `http://127.0.0.1`. Safari can block that address from the https viewer page. Use Chrome or Firefox for a local preview, or see [Troubleshooting](troubleshooting.md).
