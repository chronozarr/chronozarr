# Tile-Ripper observation outputs

This is a checkout-only Python bridge. It is not included in the published
Chronozarr 0.5.1 packages.

`convert_tile_ripper` reads a completed Tile-Ripper scientific COG fetch output
and feeds the existing Chronozarr encoder. It writes the playback store directly,
without first writing a stacked Zarr copy. No Rust Chronozarr encoder or new
Python dependency is required.

```python
from chronozarr import convert_tile_ripper

report = convert_tile_ripper(
    "hls-fetch.zarr",
    "hls-playback",
    bands=["B04", "B8A"],
    quality_policy="hls_fmask_clear",
)
print(report)
```

Then use the existing commands:

```sh
chronozarr validate hls-playback
chronozarr preview hls-playback
```

For that HLS example, the fetch must select B04, B8A and Fmask and request the
`hls_fmask_clear` policy. Tile-Ripper writes the derived quality mask on the Fmask
observation, separately from the reflectance observations. The bridge joins it
to the science scene by source identity, time and grid. Missing or ambiguous QA
is an error. It does not infer a cloud mask from reflectance or scene cloud cover.

Leaving `quality_policy=None` keeps source/product validity and AOI inclusion;
it does not screen clouds or silently impose a finite-only quality policy.
Source-valid infinities stay valid unless an explicitly selected policy excludes
them. NaN samples are kept under an invalid mask. A policy is selectable only when its mask was written
by the fetch. The requested science bands keep their stored dtype and their
individual scale, offset and units. Calibration must remain fixed over time
for each band. Physical values are computed by readers from those band metadata.

The final store has one shared validity plane. A pixel is valid only when every
selected science band is valid and included in the AOI, and the selected quality
policy accepts it. Adding another science band can therefore reduce the valid
area. Raw level-0 samples, including samples under that mask, remain stored.
The direct bridge preserves invalid Float32 NaN payloads; the generic
`chronozarr convert` xarray route instead normalizes NaNs to fill.

The input must be a local completed `acquisition_result` from the qualified
per-observation Zarr v2 COG writer, using its ordinary uncompressed, C-order,
unfiltered output. Optional compressed outputs and other writer schemas require
their own bounded adapter. The selected observations must have one exact native grid
and one collection. Science bands share one dtype; joined QA keeps its own dtype.
Times must be unique at millisecond resolution, with every
requested band at every time. There is no resampling, mosaicking, compositing,
deduplication or implicit choice between overlapping scenes. Use `groups=[...]`
to explicitly select observation groups when a fetch contains other grids or
series; include the matching QA groups too.

`max_timestep_bytes` limits admitted timestep payload work (default 128 MiB).
It is not a process RSS limit; the existing encoder also has its own working
buffers and temporary spill files. The bridge never holds the entire time
series in memory and does not create another persistent cube. Metadata is
checked before encoding; payload masks, AOI consistency and input changes are
checked while reading into the encoder's temporary spill, before final store
data is written.

Provenance retains the source descriptors, including Earth Search
`object_identity` and product `object_version` pins, the chosen bands and quality
policy, and the input manifest SHA256. The manifest hash identifies that metadata
document; it is not a hash of all payload chunks. Keep the input unchanged during
conversion and retain it when raw acquisition evidence is needed.

## Matched HLS comparison

The 28 October 2026 Ucayali comparison used the same July 1–30 2025 interval,
eight HLS S30 v2 dates, 32 pinned source assets, native AOI/grid, B03/B04/B8A and
Fmask, QA and encoder settings on both paths. With sixteen-way source acquisition
across scenes and an actual sixteen-request HTTP ceiling, ordinary Python
completed in 4.66 seconds median and Tile-Ripper plus this bridge in 5.06 seconds
(three fresh processes each). Earlier Python scene scheduling took 9.48 seconds
in interleaved controls. This is an end-to-end workload observation; public
network conditions were not controlled.

All nine timed outputs preserved stored values, masks, calibration, coordinates,
analysis and provenance. Browser queries passed all eight dates on both paths.
Tile-Ripper retained lower sampled peak process-tree RSS (209 versus 278 MiB)
and an extra raw archive of about 36 MiB. The direct Python path feeds the same
encoder without that archive. A persistent archive or bounded acquisition can
be useful requirements; the conversion path alone is not an independent user
adoption result. Use `tile-ripper stack` only when a scientific cube is required.

[Full experiment, request traces and receipts](https://github.com/chronozarr/tile-ripper/blob/codex/hls-benchmark-evidence/docs/stack-review/hls-river-parallel16.md)
are proposed in Tile-Ripper's separate evidence branch. The earlier native speed
lead used a Python recipe with less metadata/acquisition parallelism; it is not
the current comparison. Neither project is released or deployed by this work.
