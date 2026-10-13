# Sentinel-2 time series for your area

You have a bounding box or a GeoJSON polygon and a range of dates. This guide takes you from there to a chronozarr store of monthly Sentinel-2 composites that you can preview in your browser, share and publish. It uses the Sentinel-2 example in `examples/sentinel2_pc/`, which searches Microsoft Planetary Computer. You need no account and no API key.

The [example README](../examples/sentinel2_pc/README.md) is the full option reference. This page needs a checkout of the repository; [Convert your data](convert.md) is the route for imagery that you already have.

## What you get

One chronozarr store for your area:

- One time step per calendar month that has at least one Sentinel-2 L2A scene under 80 % scene cloud cover. A month with no such scene has no time step.
- Four bands at 10 m, in the UTM zone of the centre of your box: B02 (blue), B03 (green), B04 (red) and B08 (near infrared). Values are digital numbers (uint16); reflectance is the value times 0.0001.
- A multiscale pyramid (10 m, 20 m, and coarser levels for larger areas), a `coverage` plane that counts the valid scenes behind every pixel, and a `provenance` record that states how the composite was made.
- The monthly GeoTIFFs the store was encoded from, one file per month. QGIS and GDAL read them.

What this is not:

- It is not a general remote-sensing processor. It builds one product: a monthly median composite of four Sentinel-2 L2A bands. It has no other sensors, no other Sentinel-2 bands (B05 to B07, B8A, B11, B12), no other composite (no mean, no least-cloudy pixel), no sub-monthly steps and no atmospheric correction of its own. The L2A surface reflectance comes from the catalogue.
- The cloud mask is the Sentinel-2 Scene Classification Layer (SCL). It does not exist for Landsat or Sentinel-1, and the mask cannot be swapped for another one.
- Your area is its bounding box. A polygon is reduced to the box around it, and pixels of the box outside the polygon are kept.
- It does not clip, mosaic by tile or choose a projection for you beyond the UTM zone of the box centre.

## Prerequisites

- A checkout of the repository. The example is a set of scripts in the repository, not an installed command. Run `git clone --depth 1 https://github.com/chronozarr/chronozarr` and work from its root.
- [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer (the repository pins 3.13 in `.python-version`).
- `uv sync --extra ingest`, which adds `rasterio`, `pystac-client`, `planetary-computer` and `pyyaml`.
- Network access to `planetarycomputer.microsoft.com` and the storage hosts it signs URLs for.
- Disk for the monthly files and the store: 12 months of a 5 km box took 19.5 MB plus 25.2 MB, and one month of a 27 km box 45.3 MB plus 62.1 MB (see [Resource budget](#resource-budget)).

## Define your area

Pick one.

### A bounding box

Four numbers in degrees, west south east north, in WGS84 longitude and latitude, and a name:

```bash
--bbox -106.71 35.06 -106.66 35.11 --name rio_grande
```

`--bbox -106.71,35.06,-106.66,35.11` (one comma-separated value) also works. A box whose west edge is east of its east edge, one that crosses the antimeridian, and one beyond 80 S or 84 N is refused with a message that says what to fix.

### A GeoJSON file

Longitude and latitude, as RFC 7946 requires: a `Polygon`, `MultiPolygon`, `Feature`, `FeatureCollection` or any geometry. The area is the bounding box of every coordinate in the file:

```bash
--aoi-file reach.geojson            # the name is the file name without .geojson: "reach"
--aoi-file reach.geojson --name rio_grande
```

A file whose numbers fall outside longitude and latitude (a projected CRS, for example) is refused, and so is a file with no coordinates.

### The name and the grid

The name becomes the directories `mosaics/<name>` and `stores/<name>` under `--out-dir` and the id of the STAC catalog. Use letters, digits, `_`, `-` and `.`.

You do not choose a projection. The run uses the standard UTM zone of the centre of the box (EPSG 326xx north of the equator, 327xx south of it), the same rule that gives the EPSG code of every entry in `aois.yaml`. The grid is the box snapped outwards to whole 10 m pixels. If the box crosses a zone boundary, scenes from the other zone are warped onto the grid, which is slower and not bit-identical to a native read.

A 4.7 by 5.6 km box is a 466 by 563 pixel grid. A 27.8 by 28.2 km box is 2,783 by 2,821 pixels and needs 2.8 GB of memory for a 25-scene month when it is composited whole (measured below). Start small.

Do not reuse a name for another box. A month whose file exists is skipped, not rebuilt. The run checks the first existing month against your box and stops before it searches if the grids differ. Use another `--name` or `--out-dir`, or delete the old `mosaics/<name>` directory.

## Run

First a small run: a 5 km box, three whole months, into a scratch directory.

```bash
uv run python examples/sentinel2_pc/ingest.py \
    --bbox -106.71 35.06 -106.66 35.11 --name rio_grande \
    --start 2024-06-01 --end 2024-08-31 \
    --out-dir /tmp/s2_try --diagnostics
```

It searches the catalog, reads the parts of each scene that lie inside the box, writes three monthly files, encodes the store and prints the bottleneck. On the measuring machine this took 12 s once the environment had run before (31 s on the first run after `uv sync`). Check the result:

```bash
uv run chronozarr validate /tmp/s2_try/stores/rio_grande/chronozarr
uv run chronozarr info /tmp/s2_try/stores/rio_grande/chronozarr
uv run chronozarr preview /tmp/s2_try/stores/rio_grande/chronozarr
```

`validate` prints `conforms to chronozarr 0.3.0`. `info` lists the times, the bands and the pyramid. `preview` serves the store on `127.0.0.1` and opens the viewer; Ctrl-C stops it (see [Preview, share, publish](#preview-share-publish)).

Then the full range. Give both dates, begin on the first day of a month and end on the last day of one:

```bash
uv run python examples/sentinel2_pc/ingest.py \
    --bbox -106.71 35.06 -106.66 35.11 --name rio_grande \
    --start 2017-01-01 --end 2025-12-31
```

Both dates are inclusive. Without them the run uses 2015-07-01 to 2026-04-01, which is the whole archive of the example and ends in a month that holds only scenes from April 1. If a range starts after the 1st or ends before the last day of a month, the run logs a warning naming that month: it is composited from the scenes inside the range only, yet stored and carried forward like a whole month.

To add later months, run the same command with a later `--end`. Months that exist are skipped. A store is immutable under this script, so delete `stores/<name>/chronozarr` first and it is encoded again from all the months.

## What the composite means

For each calendar month, per pixel and per band:

1. Scenes. Every L2A scene that intersects the box and has `eo:cloud_cover` below 80 (percent of the scene, not of your box). Two processing versions of the same acquisition count once. All the scenes of a month enter the same median, including the overlap of neighbouring tiles.
2. Cloud mask. A scene counts at a pixel when its SCL class there is 4 (vegetation), 5 (not vegetated), 6 (water), 7 (unclassified) or 11 (snow or ice), and none of the four bands is 0. Classes 0 (no data), 1 (saturated or defective), 2 (dark area), 3 (cloud shadow), 8 and 9 (medium and high probability cloud) and 10 (thin cirrus) are dropped. SCL is 20 m and is read at the nearest source pixel for each 10 m pixel. The mask is the classifier's output, so some cloud, haze and shadow pass it and some clear pixels are dropped.
3. Median. The median of the valid values of the scenes. With an even count it is the mean of the two middle values, rounded down. Nothing is averaged over time.
4. BOA offset. Scenes of processing baseline 04.00 and later (acquired from 2022-01-25, plus some older ones ESA reprocessed) store reflectance with a +1000 offset. The run subtracts 1000 from those scenes, chosen by `s2:processing_baseline` per scene, so a series that spans 2022-01 does not jump. The stored values are digital numbers with the offset removed.
5. Carry-forward. A pixel with no valid scene in a month takes the value of the latest earlier month in the store. A pixel with no earlier value stays 0, which is the nodata value. Carried values are old observations, not measurements of the month.
6. Coverage. The `coverage` plane is the number of valid scenes behind the pixel that month, capped at 255. It is 0 where the value was carried forward or is missing. To use only real observations, keep pixels where `coverage > 0`.

A month in which no scene had a valid pixel in your box is still written. All its values are carried forward and its coverage is 0.

The composite is a per-month statistic of what Sentinel-2 saw under the SCL mask. In persistent cloud it is mostly carried-forward history; check `coverage` before reading a change between two months as real.

## Bands and band roles

| Band | Role (`common_name`) | Scale |
|---|---|---|
| B02 | blue | 0.0001 |
| B03 | green | 0.0001 |
| B04 | red | 0.0001 |
| B08 | nir | 0.0001 |

The store records the role of each band, and the viewer builds its products from the roles. `uv run chronozarr bands STORE` lists them and which products are available. For this store all six are: True color, False color, NDVI, NDWI, Water and Single band. The products are band math in the viewer; no index is stored.

## Resource budget

Measured on one machine: Apple M3 Max, 16 CPUs, 128 GB, macOS, Planetary Computer over the network available on 2026-10-12 in the evening (bandwidth not measured), default settings, scratch output on local disk. One run per row, so the times are single samples, not distributions. Another run of one 13-scene month took 8 s and once took 34 s; the log of the 34 s run was not kept, so the cause is not known.

| Box (grid) | Months | Scenes | Download | Encode | Wall, whole process | Peak memory |
|---|---|---|---|---|---|---|
| 4.7 x 5.6 km (466 x 563) | 3 | 34 | 9 s, 3.0 s per month | 0.2 s | 11.7 s | 0.43 GB |
| 4.7 x 5.6 km | 12 | 130 | 24 s, 2.0 s per month | 0.3 s | 27.2 s | 0.60 GB |
| 27.8 x 28.2 km (2783 x 2821) | 1 | 25 | 22 s | 0.6 s | 24.7 s | 2.77 GB |
| 27.8 x 28.2 km, `--memory 2GB` | 1 | 25 | 31 s | 0.5 s | 33.8 s | 1.80 GB |

Files written: 4.7 by 5.6 km, 12 months: 19.5 MB of monthly files and a 25.2 MB store (95 files). 27.8 by 28.2 km, 1 month: 45.3 MB of monthly files and a 62.1 MB store (145 files, four pyramid levels). The two 27 km runs wrote the same month, bit for bit.

How it scales. The 27 km box has 30 times the pixels and about twice the scenes per month of the 5 km box, and a month took 7 to 11 times as long. Two points do not make a law; time depends on the scenes, their cloud and the link. The first run after `uv sync` also spent about 17 s before its first log line (cold start; the cause was not investigated), and later runs spend 2 to 3 s importing.

For the bytes read, the [measurement log](evidence.md#planetary-computer-over-a-home-connection) has one workload with 1289 MB and 837 requests for 3 sparse months of the Yukon-Kuskokwim box (11 scenes, 0.69 by 0.34 degrees). This guide's runs did not record bytes.

### Memory

By default the run may use half of the available memory. Before it downloads anything it estimates what the largest month needs. If the whole month does not fit, it composites the month in strips of 512-row multiples across the full width of the grid. It stops, before downloading, only if a 512-row strip does not fit, and prints what each part needs. A strip costs bytes, not results: the 2 GB run above read 202 times instead of 125 and took 9 s longer, and the month is identical. `--memory 4GB` sets the budget yourself. In the 27 km run the estimate was 3.61 GB and the peak 2.77 GB; the estimate is built not to under-predict.

### Network

The run keeps up to 16 reads in flight (4 per CPU on machines with fewer than 4 CPUs) and lowers that when the host pushes back (HTTP 429 and 5xx, repeated failures). `--max-requests 8` is for a shared or limited link, and `--max-requests 32` for a high-latency one. `--performance fixed` freezes the settings so two runs are comparable.

### What limited the run

`--diagnostics` prints each setting and the reason, then the bottleneck and per-stage times. All five runs above (including the 2 GB one) reported remote reads as the limit: request slots busy 71 to 97 % of the run, CPU use 2 % to 4 %.

## Output layout

```
<out-dir>/                      default: <repo>/data
  mosaics/<name>/YYYY-MM.tif    one tiled GeoTIFF per month: B02, B03, B04, B08, then the valid-scene count
  stores/<name>/chronozarr/     the chronozarr store
  stores/<name>/stac/           only with --stac: collection.json and the item
```

The monthly files carry in their metadata the number of scenes searched, the scenes that could not be read, and which month filled the gaps. Keep them: a re-run resumes from them, and QGIS and GDAL read them directly (`chronozarr export-cog STORE` writes COGs from the store instead).

## Preview, share, publish

1. Preview. `uv run chronozarr preview STORE` serves the store on `127.0.0.1` and opens the hosted viewer at chronozarr.org, which reads your store from your machine. Chrome asks permission before a public page may reach a local address; allow it. If the browser blocks the read, the viewer says that it could not read the store on this computer. Use `--no-open` to print the URL, or `--viewer-dir` for a self-hosted viewer that needs no internet.
2. Share. `uv run chronozarr share STORE` makes a temporary public link through a Cloudflare quick tunnel while your terminal stays open. It needs `cloudflared`; see [Share a temporary link](share.md). Anyone with the link can read the store.
3. Publish. `uv run chronozarr publish STORE --destination s3://BUCKET/PREFIX` uploads to your own S3, R2, Google Cloud Storage or Azure bucket and prints a viewer link after `doctor` checks pass. See [Publish a permanent link](publish.md).
4. Catalog. Add `--stac` to the ingest (or run `chronozarr stac STORE`) to write a static STAC Collection and Item. Its description carries the line "Contains modified Copernicus Sentinel data."

## Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| `error: no Sentinel-2 L2A scenes ... intersect bbox [...]` | The search found nothing: open ocean, a box given in the wrong order or units, or dates with no acquisitions | Check the order (west south east north) and the dates |
| `... is on a different grid ... Months that exist are skipped, not rebuilt` | Months under `mosaics/<name>` came from another box | Another `--name` or `--out-dir`, or delete the old directory |
| `stores are immutable, delete it to re-encode` | `stores/<name>/chronozarr` already exists | Delete that directory to encode again |
| `west (...) must be less than east (...)` | Order is west south east north; or the box crosses the antimeridian | Fix the order, or split the box in two |
| The run stops with `error: ... smallest strip ... needs about ... GiB ... Nothing was downloaded` | A 512-row strip does not fit the memory budget | Pass `--memory` with the size it names if that much is free, or use a narrower box (a strip always spans the full width) |
| A scene fails after six tries over about a minute; the run stops at that month | The host kept failing. Nothing is written for that month, earlier months stay | Run the same command again; it resumes at that month. `--keep-going` writes the month without the failed scenes and lists them in `scenes_failed`; encoding then needs `--keep-going` too and names the months in the store's provenance notes |
| Long pauses with `Could not resolve host` | A failed name lookup is cached by the HTTP library for about 30 s; the run waits 31 s before the retry | Wait; it does not lower the read concurrency |
| `These months filled their gaps from a different month than the one before them` | A month was re-run or added after its successor was built | Delete the months it names and the ones after them, then re-run |
| `N months were written without some scenes` | An earlier `--keep-going` run | Delete those months and re-run to retry, or encode with `--keep-going` |
| The viewer says it could not read the store on this computer | The browser blocked the local store, or the preview stopped (see Preview) | Allow local network access, check that `preview` still runs, or use `--viewer-dir` |
| Ctrl-C during a download | Files are written to a temporary name and renamed when complete | Run the command again; no partial month is left |

`audit_scenes.py` looks its `--aoi` up in `aois.yaml`, so it does not accept a box defined with `--bbox`; to audit your area, add an entry under `aois:` (before the `# Bands and cloud mask` comment) with its `bbox` and its `epsg`. The scripts in `examples/water_masks` and `scripts/reencode_aoi.py` take `--aoi NAME` and read `<repo>/data/mosaics/NAME`; they find a box's months only when the ingest used the default `--out-dir`. This was not tested here.
