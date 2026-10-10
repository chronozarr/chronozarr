# PNG frames to a store

`chronozarr convert` turns a sequence of georeferenced PNGs, one per date, into a scrubbable chronozarr store. There is no GeoTIFF step. Exporters hand over such frames: Earth Engine thumbnails, QGIS "Save as image" with its world file option, matplotlib figures, and drone and orthomosaic pipelines.

`convert` reads the frames with GDAL, so everything it does for COGs applies. That covers the manifest, time order, `--dry-run`, `--resume`, the validity rules and the pyramid.

The store holds exactly the values in the PNGs. A rendered image holds display values, so no value in the store is a measurement. Read [Limits](#limits) before you use a store for anything but looking.

## Locating the frames

A PNG carries no georeferencing of its own. GDAL finds it in a sidecar file next to the frame. `convert` needs one of these three setups:

| Frame has | Holds | Needs `--crs` | Needs `--bounds` |
|---|---|---|---|
| a world file, `2019-01.pgw` or `2019-01.wld` | the transform | yes | no |
| `2019-01.png.aux.xml` | the CRS and the geotransform | no | no |
| neither | nothing | yes | yes |

```bash
# a world file per frame: the transform is there, the CRS is not
uv run chronozarr convert frames/manifest.csv out --crs EPSG:32718

# an .aux.xml per frame (GDAL writes one when it copies a georeferenced raster to PNG)
uv run chronozarr convert frames/manifest.csv out

# no sidecar: say where the frames are
uv run chronozarr convert frames/manifest.csv out --crs EPSG:32718 \
    --bounds 498450,9151960,508690,9162200
```

The manifest has `uri,datetime` rows in CSV or JSON. Relative URIs resolve against the folder of the manifest.

A few rules apply to all three setups:

- A world file gives the center of the upper-left pixel. GDAL accounts for that, so you do nothing. `--bounds` takes the outer edges of the image.
- `--crs` is the target CRS and also the CRS of any frame that declares none. If the frames carry a CRS and `--crs` names a different one, `convert` warps the frames. The warp needs `--resampling`.
- `convert` finds the sidecars of `http(s)` PNGs too, at the cost of a few extra requests per frame.
- A frame with a geotransform but no CRS fails when `--crs` is missing. The error names the first such frame.
- A frame with no georeferencing at all fails. The error lists the three fixes in the table.

### `--bounds`

`--bounds west,south,east,north` is the extent of every frame, edge to edge. Its units are those of `--crs`: meters for UTM and degrees for EPSG:4326.

`convert` derives the north-up transform from the pixel size of each frame: `(east - west) / width` by `(north - south) / height`, so the pixels need not be square. A JSON manifest can hold the same extent at the top level:

```json
{"bounds": [498450, 9151960, 508690, 9162200], "items": [{"uri": "2019-01.png", "datetime": "2019-01-01"}]}
```

Give the extent once, either in the manifest or on the command line. `convert` refuses `--bounds` in these cases:

- `--crs` is missing.
- West is not less than east, or south is not less than north.
- A frame has a different size from the first. The same extent over a different pixel count gives a different pixel size, and `convert` does not guess.
- A frame carries its own geotransform from a world file or an `.aux.xml`. Drop `--bounds` or remove the sidecar.
- A frame declares a CRS that is not `--crs`.

Check that the extent you pass is the extent of the rendered image. An exporter that pads or crops the region you asked for moves every pixel by that amount. `convert` cannot detect this from the PNG.

## What the frames become

| In the PNG | In the store |
|---|---|
| red, green and blue channels | bands `red`, `green` and `blue` with those `common_name`s, scale 1, offset 0 and no units |
| alpha channel (RGBA, gray + alpha) | the `mask` variable, 1 where alpha is nonzero. The alpha channel is not a data band. |
| gray channel | band `1` |
| 8-bit values | `uint8` data. The viewer shows them as they are. |
| palette (indexed color) | refused. See [Palette PNGs](#palette-pngs). |

The viewer picks products by band name. The three color bands enable True color. False color, NDVI, NDWI and Water need a near-infrared band, so they stay off. Single band is available.

A manifest can name the bands in a `bands` column or key. Those names replace `red`, `green` and `blue`, and the bands then have no common names. Pass `--band-role NAME=red,...` to `convert` to give them (see [Band roles](python.md#band-roles-and-the-first-view)).

### Alpha

Alpha wins over everything else, as in GDAL. A pixel is valid where alpha is nonzero. A partly transparent pixel (alpha 128) is valid and is drawn fully opaque, because the mask is 0 or 1. The store keeps the RGB values under alpha 0, and the mask hides them.

Without an alpha channel the store has no mask and no nodata. Every pixel is valid, and a stored 0 is data.

### Palette PNGs

A palette PNG fails with the way to expand it:

```
cannot open source .../frame.png: it is a palette (indexed colour) PNG, so its values are palette
indices, not colours. Expand it to RGB first, for example `gdal_translate -expand rgba in.png out.png`,
or save the frames as RGB
```

`convert` does not expand a palette PNG by itself. The indices of a classified map are the data, and the converter cannot tell a class map from a picture. This rule applies to PNG only. `convert` reads a GeoTIFF with a color table and stores its indices as the values.

## Worked example: Ucayali, 36 months

`examples/png_frames/` turns the monthly Sentinel-2 mosaics in `data/mosaics/` into frames, the way an exporter would. The ingest in `examples/sentinel2_pc` writes the mosaics. The example then converts the frames:

```bash
uv run python examples/png_frames/render_frames.py      # 36 PNGs + world files + manifest
examples/png_frames/convert.sh                          # convert --crs EPSG:32718, then validate
uv run python examples/png_frames/check_store.py        # every frame bit-exact in the store
cd js && node ../examples/png_frames/viewer_check.mjs   # headless viewer, repo served on :8000
```

The convert step is one command:

```bash
uv run chronozarr convert data/png_frames/ucayali/manifest.csv \
    data/stores/ucayali_santa_maria/png-1 --crs EPSG:32718
```

The frames have these properties:

- They cover 2019-01 to 2021-12.
- Each frame is a 1024 x 1024 pixel window (row 768, column 1280) of the 2765 x 2759 mosaic. The pixels are 10 m in EPSG:32718, and the PNG is 8-bit RGBA with red = B04, green = B03 and blue = B02.
- Alpha is 0 where the `coverage` of the mosaic is 0, which means no valid scene that month. The RGB values are 0 there too.
- One fixed linear stretch applies to all frames. It maps the 2nd and 98th percentile of the valid red, green and blue values of the whole series to 0 and 255. The viewer's tone mapping works on reflectance and is a different map.
- The stretch is the same for every month, so brightness compares across months.

The store is larger than the PNGs. It adds random access and a pyramid. Sizes, times and the checks run on this example are in [evidence.md](evidence.md#png-frames).

## Limits

- A rendered PNG holds display values. The store has no units and scale 1, so the index products stay off. If the data behind the picture matters, convert the data: COGs or Zarr.
- Each channel has 8 bits, which gives 256 levels. The stretch of the exporter is baked in, and whatever it clipped or compressed is gone. The viewer shows 8-bit color as it is and does not stretch it.
- Pyramid levels are block means of the stored values. They average display values, so they are wrong for radiometry.
- The mask is binary. Soft alpha, such as anti-aliased edges or a feathered mosaic seam, becomes opaque wherever alpha is above 0.
- The series has one grid. `--bounds` needs every frame to have the same size. Without `--bounds`, `convert` warps frames with different world files or sizes onto the grid of the first frame with `--resampling`, as it does for COGs. Warped pixels with no source are masked.
- `convert` reads a PNG whole, from top to bottom, with no tiles or overviews. Memory during staging holds a few frames, so a very large PNG costs its raw size times `1 + --read-ahead`.
- `convert` looks for sidecars next to `.png` files only. Other image formats, such as JPEG with `.jgw`, get no sidecar probing.
- Only 8-bit gray, gray + alpha, RGB and RGBA PNGs are tested.
