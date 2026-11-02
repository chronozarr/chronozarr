# chronozarr 0.5.2

Python and npm **0.5.2** collect the fixes and import routes added after 0.5.1.
Existing spec 0.3 and 0.4 stores remain readable without conversion. The stored
layout, values and codecs have not changed.

Use the [quickstart](quickstart.md) for installation and a worked conversion.
The [0.5.1 release notes](release-0.5.1.md) describe the earlier conversion,
coordinate-system and viewer-distribution changes.

## Preview and sharing

`preview` and `share` now stop cleanly when Ctrl-C arrives during browser launch
or sharing setup. Preview also closes its local server when browser launch
raises an error. This repairs a shutdown race reproduced by the macOS
quickstart check.

Temporary sharing still requires a network that permits Cloudflare quick
tunnels. A DNS or firewall block cannot be repaired by the package. Use
[local preview](preview.md) when sharing is unavailable, or
[publish to static storage](publish.md) for a permanent link.

## Browser viewer

- The pixel inspector displays and copies WGS84 latitude and longitude at the
  selected native pixel's centre. Projection code loads only when needed;
  failed clipboard access leaves the coordinates selected for manual copying.
  An older coordinate lookup cannot overwrite a newer selection.
- Large uint16 timelines can retain losslessly packed values in browser memory,
  including their masks. CPU reads and pixel queries still return the stored
  values. The viewer keeps ordinary textures when a packed texture would exceed
  the GPU's texture-size limit.
- Scrub level selection and buffer planning use tested helpers while preserving
  adaptive batch sizes and the existing complete-frame behavior.
- The public catalog adds the recovered four-band Lake Mead imagery. Existing
  catalog entries and the default selection are retained.

The hosted viewer and an installed npm viewer are separate copies. Upgrade the
npm package to obtain these changes on your own host.

## Python imports and examples

- [Icechunk selections](icechunk.md) can be converted from anonymous HTTPS or
  local repositories. Select a bounded variable, region and snapshot explicitly.
  This optional route requires Python 3.12 or newer and the `geo` and `icechunk`
  extras. Scalar selections retain the time and other required dimensions.
  Virtual GRIB Float64 data require an explicit Float32 rounding choice.
- The [Tile-Ripper bridge](tile-ripper.md) converts qualified completed COG
  observation outputs directly. It preserves source identities, calibration and
  selected quality masks on one exact native grid; it does not resample or
  silently choose among overlapping observations. It needs no new Python
  dependency.
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
