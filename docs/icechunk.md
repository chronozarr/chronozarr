# Convert an Icechunk selection

The repository checkout can convert a selected variable from an anonymous HTTPS or local
Icechunk repository into a chronozarr store. Icechunk resolves one snapshot; xarray and Zarr
read its declared codecs. The existing converter stages one selected timestep at a time and
writes the playback pyramid. There is no intermediate GeoTIFF or Zarr copy.

This route needs Python 3.12 or newer and the `geo` and `icechunk` extras. These commands are
for this checkout; this feature has not been published as a package release:

```bash
uv sync --locked --extra geo --extra icechunk
```

## One materialized GFS selection

This recorded snapshot contains the requested two times, 2026-10-25 00:00 and 01:00 UTC.
The window covers latitude 34 and 33.75 degrees and longitude -101 and -100.75 degrees.
`--isel` uses integer indexes and an exclusive stop. Give an index or finite slice for
every dimension of the chosen variable, including ensemble member or level where present.

```bash
uv run --no-sync chronozarr convert \
  https://dynamical-noaa-gfs.s3.us-west-2.amazonaws.com/noaa-gfs-analysis/v0.1.0.icechunk \
  gfs-playback \
  --icechunk --icechunk-snapshot 6K6H23PYR6QXA9VM0C6G \
  --variable temperature_2m \
  --isel time=48072:48074 --isel latitude=224:226 --isel longitude=316:318 \
  --nodata nan --dry-run
```

Run the same command without `--dry-run` to write a new store. `--nodata nan` explicitly marks
NaN as invalid; the converter writes a separate mask and preserves valid zeros. The existing
`preview`, `share` and `publish` commands apply to the resulting `gfs-playback` store.

The source's Celsius values, native WMO sphere CRS (radius 6,371,229 m), grid and CF scale and
offset are retained. The source latitude order is normalized to the converter's north-up grid.
No reprojection, temporal compositing or unit conversion is performed. A viewer reads the
converted store using its existing reader and renderer.

`--icechunk-branch NAME` defaults to `main`. A branch or tag is resolved once, before opening
arrays; all later reads use that snapshot. `--icechunk-tag NAME` and
`--icechunk-snapshot ID` select alternatives. Give only one version selector.
The dry run prints the resolved snapshot and integer selection. Source identity, snapshot,
selection, data types, original chunk shape and any explicit rounding go in output provenance.
For materialized sources, they also bind staged data to the resume fingerprint. If a branch
advances, a resumed materialized import refuses the changed source; pass the original snapshot
ID to reuse its staging safely.

A dry run samples up to three selected timesteps to estimate conversion, as ordinary
`convert` does. It can download source chunks but writes no store or staging directory.

## Virtual GRIB sources and precision

Virtual Icechunk arrays reference original GRIB messages. The `icechunk` extra installs
Gribberish's maintained Zarr codec. Each external prefix must be authorized explicitly for
anonymous reading:

```bash
uv run --no-sync chronozarr convert \
  https://dynamical-noaa-gfs.s3.us-west-2.amazonaws.com/noaa-gfs-analysis-virtual/v0.1.0.icechunk \
  gfs-virtual-playback \
  --icechunk --icechunk-snapshot SJSV5KG1GSWR2XQD2SW0 \
  --variable temperature_2m \
  --isel time=48072:48074 --isel latitude=224:226 --isel longitude=316:318 \
  --virtual-prefix s3://noaa-gfs-bdp-pds/ \
  --cast-float32 --nodata nan
```

This particular virtual variable decodes to Float64. Chronozarr does not store Float64, so
conversion fails unless `--cast-float32` explicitly requests rounding. It rounds each staged
timestep once, records the precision change, and checks source validity before rounding.
A separate validity mask preserves distinctions between source values that round to the same
Float32 value. Finite values outside the Float32 range fail. With rounding, use nodata `auto`,
`none` or `nan`; a source sentinel in its metadata is tested in the source precision.

The codec's own transforms still decode as declared by the source (for this store, the
Gribberish decode and a storage scale/offset yield Celsius). Xarray's CF `mask_and_scale`
is disabled: CF scale and offset are recorded for the resulting band, without applying them
again to the stored values.

An Icechunk snapshot pins the reference mapping. External GRIB files can still change;
this importer does not record their object identities, and the qualified virtual references
supply no ETag or modification-time binding. Source I/O and any checks on referenced objects
belong to the maintained Icechunk reader. `--resume` is therefore refused for imports that
authorize virtual prefixes, before reading data, to avoid mixing staged old values with newly
changed external bytes. Start a fresh import after a failure. Output provenance discloses this
external-identity limitation. An uninterrupted virtual import also has no archive-wide external
snapshot guarantee.

The materialized and virtual GFS products are different scientific inputs. At the recorded
window their values differ by about 0.051 degrees Celsius at most. Selecting the virtual
source is explicit; a performance result cannot establish numerical equivalence.

Only the prefixes supplied with `--virtual-prefix` are authorized. S3 prefixes receive
explicit anonymous credentials. The repository uses anonymous HTTPS (or an explicit local
path); this route does not search ambient cloud credentials. It supports S3 and HTTPS virtual
prefixes, subject to the maintained Icechunk reader's support and source access policies.

## Forecast axes and limits

For forecast variables with `init_time` and `lead_time`, select one initialization. The importer
forms `valid_time = init_time + lead_time` and uses those dates for playback. It records the
initialization and the derivation. It refuses multiple initializations with a lead-time axis;
choose a run rather than flattening overlapping forecasts. Other dimensions still require
explicit selections. This time conversion has synthetic regression coverage; the live
qualification below is for analysis products with a `time` axis.

The default admission limits are 512 MiB of selected decoded data (including an explicitly
selected source mask) and 128 MiB for any decoded source data or mask chunk. Change them with
`--max-import-mb` and `--max-source-chunk-mb`. They use MiB (1,048,576 bytes). Metadata and
coordinate reads happen before admission. These limits do not cap total downloaded bytes,
the output pyramid, codec scratch, read-ahead memory or process RSS.

Materialized imports also use Icechunk's maintained encoded-range cache, with a separate
32 MiB allowance (`--source-cache-mb`, or `IcechunkInput(encoded_cache_bytes=...)`). Set it to
0 to disable it. The cache keys include the immutable chunk ID and byte range, so repeated
timesteps from one logical time chunk can reuse the compressed payload and shard index.
It does not fetch a whole shard to fill the cache. A local HTTP regression verifies the second
timestep and a repeated first timestep need no source request with this cache, while disabled
and tiny budgets reread data. This reduces network work; decompression can still repeat.
The budget is recorded in the plan and provenance. The library does not expose occupancy or
hit counters here, and its metadata caches and decoder buffers have separate memory costs.
The cache is disabled for imports that authorize virtual sources until external object
identity is captured.

An independent live HTTP meter read the first selected time, the next time, then the first
again. With caching disabled the three reads received 1,372,181, 1,365,811 and 1,365,811 body
bytes. With the 32 MiB cache they received 1,372,181, 0 and 0 bytes, with unchanged values.
These counts start after opening metadata and compare reads from the same snapshot; they
are not total import traffic or wall-time measurements. See the
[cache receipt](../examples/icechunk/import-cache-receipt.json).

A small window still needs whole source chunks. The qualified materialized variable has
14,400,000-byte decoded logical chunks spanning 1,440 times and 50 x 50 pixels; the virtual
variable has an 8,305,920-byte decoded whole-grid chunk per time. Source shards are larger
objects; Icechunk and Zarr handle the ranges needed inside them. Neither route is promised
to be faster for every selection. The import adapter retains the source read granularity.

This is a conversion route. The published browser viewer does not open arbitrary Icechunk
repositories directly. Arbitrary codecs, curvilinear grids, variables outside Chronozarr's
type and grid rules, authenticated repositories and automatic catalog routing remain outside
this qualification.

## Reproduce the live checks

```bash
uv run --no-sync python examples/icechunk/qualify.py NEW_DIRECTORY
```

The script compares each import with its own snapshot read directly by Icechunk/xarray,
checks exact Float32 output (explicitly rounded for the virtual source), CRS and units, then
reads the resulting files over HTTP with the existing JavaScript reader and verifies a cached
timestep needs no request. It records elapsed import time but has no wire-byte counter and
makes no renderer-speed claim. Both paths use the same underlying maintained source reader;
this is a conversion round-trip check, not an independent codec implementation comparison.

To also check first-frame drawing and time switching in headless Chromium, install the
checkout's JavaScript test dependencies and run:

```bash
npm ci --prefix js
node examples/icechunk/check-viewer.mjs NEW_DIRECTORY
```

This uses the existing viewer and SwiftShader, checks complete first frames, switches
0 → 1 → 0 and checks WebGL and page errors. It adds its result and screenshots to that
local directory. This is a functional smoke check, not a speed benchmark.

The recorded result is in
[`examples/icechunk/qualification-20261027.json`](../examples/icechunk/qualification-20261027.json).
