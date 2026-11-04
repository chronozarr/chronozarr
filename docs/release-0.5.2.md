# chronozarr 0.5.2

Python and npm **0.5.2** collect the fixes and import routes added after 0.5.1.
Existing spec 0.3 and 0.4 stores remain readable with the same layout and codecs.

Use the [quickstart](quickstart.md) for installation and a worked conversion.
The [0.5.1 release notes](release-0.5.1.md) describe the earlier conversion,
coordinate-system and viewer-distribution changes.

## Preview and sharing

`preview` and `share` now stop cleanly when Ctrl-C arrives during browser launch
or sharing setup. Preview also closes its local server when browser launch
raises an error. This repairs a shutdown race reproduced by the macOS
quickstart check.

Temporary sharing requires a network that permits Cloudflare quick
tunnels. If DNS or a firewall blocks the tunnel API or address, use
[local preview](preview.md) when sharing is unavailable, or
[publish to static storage](publish.md) for a permanent link.

## Browser viewer

- The viewer shows the date of the frame on screen while a selected date is
  loading or unavailable. It updates the displayed date when the new frame paints.
- A shared link to an unavailable store shows an error and offers a way to open
  the default dataset. When clipboard access fails, the viewer shows the full
  permalink for manual copying.
- The pixel inspector displays and copies WGS84 latitude and longitude at the
  selected native pixel's centre. Projection code loads only when needed;
  failed clipboard access leaves the coordinates selected for manual copying.
  The inspector labels the selected frame's values as a snapshot and displays
  store-provided labels as text. A missing value in one band is marked missing
  even when another band has data at the same pixel.
- Pinch or use the zoom buttons on touch screens. Focus the image to zoom and
  inspect with the keyboard. Movie exports keep the product selected when
  export starts.
- Large uint16 timelines can retain losslessly packed values in browser memory,
  including their masks. CPU reads and pixel queries still return the stored
  values. The viewer keeps ordinary textures when a packed texture would exceed
  the GPU's texture-size limit.
- Scrub level selection and buffer planning now use shared helpers. Adaptive
  batch sizes and complete-frame behavior are unchanged.
- The public catalog adds the recovered four-band Lake Mead imagery. Existing
  catalog entries and the default selection are retained.

The hosted viewer and an installed npm viewer are separate copies. Upgrade the
npm package to obtain these changes on your own host.

## Conversion and append integrity

- Warped rasters carry a validity mask through resampling. A valid average can
  equal the original nodata value, while a target pixel with no contributors
  stays missing. To recover a value affected by the old warp, convert again
  from the source rasters in a fresh work directory.
- Local GeoTIFF conversion honors an external `.msk` alongside the raster.
- `encode` and `append` refuse a transform attribute that disagrees with
  the DataArray's pixel centres. Update or remove a stale attribute after
  spatial slicing. Pass `transform=` explicitly when the axes are index labels.
- Pyramid generation refuses an overview value that collides with the numeric
  nodata sentinel in a store without a mask. Use an explicit validity mask, or
  write only level 0 with `--lods 1` when making a new store.
- Raw COG exports keep their base values and omit generated overviews when a
  valid mean could collide with a per-band nodata sentinel. Use `--physical`
  for averaged overviews in that case.

## HTTP reads

- A server returning HTTP 416 for a shard payload now fails the read with the
  requested range. `doctor` reports the decode failure. A missing sparse chunk
  can still return fill.
- Python retries a truncated HTTP response and removes reflected request
  paths from HTTP status messages. Browser errors redact query strings on
  signed URLs, including IPv6 addresses.
- The browser reader escapes punctuation in array names so it can fetch the
  declared data chunks.
- Local preview uses ETags for cache revalidation. An append completed within the
  same second as a previous read can still refresh the timeline.

## Python imports and examples

- NetCDF and Zarr imports decode packed CF coordinates and honor CF integer
  signedness before applying nodata and scaling. Reconvert from the source
  if a previous import used the wrong coordinates or signed values.
- For CF imports, a variable's named grid mapping supplies its CRS. Use
  `--crs` to replace an incorrect or unreadable CRS definition.
- [Icechunk selections](icechunk.md) can be converted from anonymous HTTPS or
  local repositories. Select a bounded variable, region and snapshot explicitly.
  This optional route requires Python 3.12 or newer and the `geo` and `icechunk`
  extras. Scalar selections retain the time and other required dimensions.
  Virtual GRIB Float64 data require an explicit Float32 rounding choice.
- The [Tile-Ripper bridge](tile-ripper.md) converts completed COG observation
  outputs from the tested writer. It preserves source identities, calibration
  and selected quality masks on one exact native grid. Inputs must have unique
  times, and the bridge does not resample or choose among conflicting
  observations. It needs no new Python dependency.
- The Sentinel-1 example streams acquisitions into monthly means while retaining
  its existing mask and provenance checks.
- The water-stack checker accepts `--data-root` so a candidate can be checked
  against its own mosaics before local data are replaced.

## Release and documentation

The guides and package metadata target the same Python and npm version. The
repository includes citation metadata, issue forms and a pull-request template;
the site and demo have link-preview metadata. Documentation dependencies retain
the existing audit checks and seven-day release-age policy.

Chronozarr remains an open format and libraries with static hosting. Package
publication does not upload, replace or rewrite users' datasets.
