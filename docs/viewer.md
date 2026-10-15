# Explore the live demo

[Open the live demo](https://chronozarr.org/demo/). It loads 117 monthly Sentinel-2 composites of the Ucayali River in Peru, November 2015 to February 2026. The store is in a public storage bucket. The viewer reads it directly, with no server in between.

This page describes the viewer. Every store that you preview, share or publish opens in the same viewer. It describes the viewer at chronozarr.org/demo/. A viewer folder copied from npm package 0.4.0 (see [Self-host the viewer](viewer-distribution.md)) lacks the start rule below and the messages for a stopped share or a blocked local store; copy it again from 0.4.1.

## Where it starts

The demo opened from its plain address, `https://chronozarr.org/demo/`, starts at the latest well-covered timestep. Among the newest 24 timesteps, that is the latest in which at least half of the pixels are observed, according to the store's coverage plane, or valid, according to its mask. A store with neither plane starts at its last timestep. The public Ucayali store has neither, so it starts at its last month.

Any other address starts where it says. A link with a timestep (`t`) opens at that timestep. A link with a store, a product, a zoom or a centre but no `t` opens at the first timestep, as such links always have. A store that you preview, share or publish opens at its first timestep unless its link says otherwise.

## Controls

| To | Do this |
|---|---|
| Play or pause | Click the play button, or press Space |
| Step one timestep | Press the Left or Right arrow key, or click the arrows beside the date |
| Jump to a date | Click or drag on the timeline |
| Go to the first or last timestep | Focus the timeline, then press Home or End |
| Change the playback speed | Drag the speed slider |
| Zoom | Scroll, or pinch on a touch screen |
| Pan | Drag the map |
| See a pixel's values over time | Click the map. The inspector shows the values and a chart. Press Escape to close it |
| Change the product | Choose it in the product list, or press 1 to 9 |
| Change the display range of one band | Choose Single band, then set the minimum and maximum. Auto fits the range to the pixels on the screen |
| Color one band | Choose Single band, then pick Viridis, Magma or Turbo in the colormap list. The bar between the minimum and maximum shows the colors |
| Show where no scene was observed | Click Gaps. It hatches the pixels whose value was carried over from an earlier month. It needs a store with a coverage plane |
| Copy a link to this view | Click Copy link |
| Open another store | Paste its URL in the store field and click Open |
| Save the loop as a video | Click Export, choose MP4, WebM or GIF and the range, click Export, then click the Download link. Firefox records WebM and GIF only. Embedded viewers have Export too |
| Show load and decode statistics | Press D |

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

Copy link copies the address of the current view. It holds the store, the timestep, the product, the band with its display range and colormap, the zoom and the map centre. It does not hold the playback speed or the loop setting.

The address uses these parameters:

| Parameter | Meaning | Example |
|---|---|---|
| `store` | URL of the store | `https://data.example.org/my_store` |
| `t` | Timestep index, from 0 | `t=71` |
| `p` | Product | `p=ndvi` |
| `b` | Band, for Single band | `b=B08` |
| `r` | Display range low,high in physical units, for Single band | `r=0,0.4` |
| `cm` | Colormap for Single band: `gray`, `viridis`, `magma` or `turbo` | `cm=viridis` |
| `z` | Zoom, in screen pixels per stored pixel | `z=2.5` |
| `c` | Map centre x,y in the store's coordinate system | `c=684500,9123350` |

`chronozarr link STORE_URL` prints the same kind of link from the command line and checks it against the store. To show the viewer inside your own page, see [Embed the viewer](embedding.md).

## What the browser downloads

The map is divided into cells of 512 by 512 pixels. For each view, the browser downloads the cells on the screen, at the current timestep, at the level of detail that matches the zoom. It also downloads the timesteps around the current one in the background. Stepping through time then usually needs no new download. A link to a store gives read access to the whole store, but the browser fetches only what the view needs.

## Privacy

The viewer page and its scripts come from chronozarr.org. The page runs no analytics script and sets no cookies. It keeps the playback speed that you choose in your browser's local storage. The store's data travels from the store's host to your browser, and chronozarr.org receives none of it. The page address, with its `store=` value, does reach chronozarr.org's host, as any page request does. [Private stores](private.md#1-what-a-viewer-link-exposes) explains what a viewer link exposes.

The documentation pages are separate from the viewer. They load their fonts from Google Fonts and use Cloudflare Web Analytics.

## Browser support

The viewer needs WebGL2. Current versions of Chrome, Edge, Firefox and Safari have it. A store that you preview from your own computer is served from `http://127.0.0.1`. Safari can block that address from the https viewer page. Use Chrome or Firefox for a local preview, or see [Troubleshooting](troubleshooting.md).
