# chronozarr 0.5.1

Python and npm **0.5.1** bring the changes since 0.4.1 together with sixteen review repairs. They add more raster inputs, custom coordinate systems and a bundled viewer, while retaining support for existing v0.3 stores.

Install 0.5.1 with the [quickstart](quickstart.md), or use the [source and local artifact routes](development.md). The [0.4.1 documentation](https://github.com/chronozarr/chronozarr/tree/v0.4.1/docs) remains available for that older release.

## Conversion and source metadata

- Read GDAL raster formats through rasterio, including BIL/FLT sidecars, ASCII grids, GRIB and selected HDF subdatasets, and archives containing one raster. The converter preserves a source's native CRS, including a CF grid mapping from NetCDF or Zarr.
- Read year/day-of-year names with `--date-pattern` and `%j`; read an Earth Engine band-per-date export with `--bands-are-dates`; join nonoverlapping same-date tiles with `--mosaic-tiles`. Preflight explains duplicate, overlapping and incompatible inputs before writing.
- Convert a folder or glob of NetCDF files as one time series, and squeeze length-one dimensions while retaining their coordinate identity and units. Calendar mapping is explicit through `--calendar-to-gregorian`. The `netcdf` extra includes cftime; a separate `cftime` extra supports climate-calendar Zarr inputs.
- Report units, physical ranges and likely undeclared fill codes in the plan. Strong fill evidence and likely divisor scales require an explicit choice before conversion. `--nodata`, `--scale` and `--offset` record that choice; scale and offset replace their source attributes independently.
- Normalize date-bearing file labels used as one-band descriptions, retain meaningful band names, and write an editable manifest even when preflight finds problems. `append` also accepts a single GeoTIFF path.

See [Convert your data](convert.md) for supported forms, refusal rules and examples. Writers now store all-fill chunks too; readers continue to interpret absent chunks and empty shard entries as the declared fill. Explicit all-fill objects can increase sparse-store request counts and storage.

## Reading, CRS and store compatibility

Python can open recognized foreign Zarr multiscale layouts without a chronozarr block and read their declared chunking, values and validity. It refuses band identity, missing-marker or physical-encoding combinations that its shared band model cannot represent. Chronozarr readers distinguish decoding from writer conformance: readable departures are reported as notes, while `chronozarr validate` checks the profile.

The JavaScript reader assembles supported declared time, band and spatial chunk layouts, independent mask and coverage chunks, alternative chunk keys and byte orders into consistent logical cells. Unsupported dtypes, codecs and shard-index forms fail explicitly. Standard one-cell layouts retain worker decoding and compressed caching; general layouts use the calling-thread adapter and retain decoded cells only. Their performance is not covered by standard-layout speed gates.

CRS identity follows WKT2, then PROJJSON, then an authority code. An equal lower-priority code cannot hide differing definitions. The MapLibre adapter can use supported non-EPSG projection definitions through its lazy proj4 build; unsupported projections and datum-grid requirements remain limits described in the [layer guide](../js/maplibre/README.md#projections).

Python's `link --lonlat` uses pyproj from the `geo` extra to interpret a stored WKT2/PROJJSON definition or prove it equivalent to its code. This explicitly narrows the parser-free shortcut to code-only `EPSG:4326` stores: redundant WKT2 on a geographic store still needs pyproj, and a conflicting definition never inherits the shortcut from a lower-priority code. `--xy` and links without a centre continue to work without pyproj. Refusals name the CRS briefly and explain the missing extra.

| Store | 0.5.1 behavior | Released 0.4.1 behavior |
|---|---|---|
| EPSG CRS, spec `0.3.0` | Reads and writes this version | Reads the original supported layouts |
| Other CRS, spec `0.4.0` | Reads and writes this version | Refuses this version |
| Existing ordinary spec `0.3.0` store | Reads without conversion | Continues to read |
| Legacy spec `0.2` store | Convert with `chronozarr convert OLD_STORE NEW_STORE` | Conversion is also required |

Package versions and store specification versions are independent. Opening an existing store with 0.5.1 does not rewrite it. Supported `0.3.x` and `0.4.x` patch versions remain readable, with a note for an unknown patch. Writer validation enforces the CRS/version rule. Zarr `3.1.6` is the minimum dependency; Python still requires 3.11 or newer.

## Viewer, links and distribution

The viewer adds a dual-polarization SAR composite, single-band Viridis/Magma/Turbo colormaps, viewport auto exposure for numeric and radar data, and MP4 export where the browser supports it. Embedded viewers retain export controls. Copy link and the Python player carry the selected display state; `chronozarr link` can set colormap, zoom and a centre in store coordinates or longitude/latitude.

The npm tarball contains a production viewer bundle with a viewer script, decode worker and lazy codecs/projection parser. Catalog and root metadata requests start early. Bandwidth-aware playback and shared speculative request budgets adapt to slow links. Benchmark warm loops count their actual requests and bytes; the repository has paired speed-gate checks rather than treating a warm loop with traffic as a cache hit.

The Python package, npm package, dataset host and viewer served at chronozarr.org are separate. Installing either package does not update the hosted demo. Use the npm 0.5.1 [viewer bundle](viewer-distribution.md) to self-host the same viewer.

## Sharing, publication and development tools

`share` validates its local copy and checks browser access through the tunnel. Doctor probes and remote validation batch independent work, and object sizing can use a bounded GET when a host refuses HEAD. Cancellation stops a hanging doctor check, and link startup handles quick-tunnel DNS waits. Incremental publication compares canonical array metadata before uploading an appended store, even when optional summaries or consolidated metadata are absent.

Repository examples add NISAR GCOV time-series and Sentinel-1 RTC monthly ingest. The corpus runner accepts a separate results directory and records its evaluation policy; longitude spelling outside −180 to 180 is a diagnostic rather than a correctness failure by itself. Quickstart and newcomer harnesses exercise printed instructions, and raw Markdown/llms pages and the repository skill make the guides available to coding agents. These source examples and harnesses have their own dependencies and operating limits.

The documentation site uses patched TOML, Vite, DOMPurify and KaTeX dependencies. Its build prepares Mermaid's browser bundle from the installed modules so diagrams receive those patches too; updating the dependency graph alone does not replace libraries embedded in an upstream bundle. Compatibility checks cover frontmatter, math and rendered diagrams. The [site maintenance guide](../site/README.md) describes the preparation step and pinned versions.

## Release review of documentation and examples

The [review record](release-0.5.1-review.md) lists coverage, findings, fixes and validation limits.

Both SAR examples now bind cached pixels to a CRS, transform and dimensions. A mismatched or unverified legacy cache stops before extraction or overwrite, with instructions to choose a new cache. NISAR selects the series with the most distinct acquisition times rather than counting reprocessed versions as extra dates.

The example guides explain credentials, pin the recorded NISAR query, scope the sample-specific audit helpers and avoid applying a second offset correction to current Sentinel-2 mosaics. The unsupported SWOT intensity live-check mode now refuses the request instead of opening unrelated imagery.

Documentation distinguishes native observation counts from rounded overview coverage, preserves finite values under a validity mask, and explains that signed store queries are removed from the embedded wordmark link. `serve_store` is documented as local-only. The current public Ucayali imagery store has no mask or coverage plane; dated historical measurements retain their original scope.

## Repairs from the merged-batch review

| Finding | Corrected behavior |
|---|---|
| 1 | Publication compares canonical geometry, CRS, shape, dtype, fill, dimensions and codecs for data, mask and coverage when optional mirrors or consolidation are absent. Compatible append remains eligible. |
| 2 | Foreign bands with different missing markers in one level are refused before they can share an incorrect validity rule. |
| 3 | Foreign overview levels with different effective scale or offset are refused when one shared band encoding cannot represent them. |
| 4 | Foreign overview levels must keep band count, names and order. |
| 5 | NetCDF series preflight retains squeezed coordinate presence, values and units; a changed depth or realization is refused before output. |
| 6 | The JavaScript reader reports harmless profile departures as notes, reads supported general chunk layouts and uses consistent logical cell dimensions across levels for GPU rendering. |
| 7 | COG export uses the selected overview's nodata marker, preserving valid values equal to another level's marker. |
| 8 | Installation pins, command examples, API references and viewer instructions all target 0.5.1. The earlier candidate record retains its original scope. |
| 9 | Fill detection retains the strongest observed pixel share across bands, so weaker isolated evidence cannot suppress a required refusal. |
| 10 | Sharded HTTP reads share HEAD-refusal fallback and refresh indexes for versionless mutable objects, including validity planes. |
| 11 | The corpus evaluator no longer marks longitude convention alone as a value or placement error; genuine failures remain failures and historical records are preserved. |
| 12 | Explicit NetCDF scale and offset overrides take effect independently before compatibility and numeric validation; original declarations remain explained. |
| 13 | A replacement prefetch plan claims both data and mask work and waits for the required planes before reporting completion. |
| 14 | JavaScript compares authoritative CRS parameters, datum, ellipsoid, axes and units; stale root authority annotations cannot replace actual definitions. |
| 15 | Python retains WKT2/PROJJSON precedence with or without optional pyproj, instead of accepting conflicts through a shared code. |
| 16 | Writer validation checks CRS/profile version conformance while tolerant readers preserve decoding and supported patch-version behavior. |

## Validation and publication status

Release preparation starts from merged commit `63610f8` (PR #141). The Python and JavaScript runtime source is the tested `0.5.0rc1` candidate promoted to version 0.5.1. Release metadata, current installation guides, site navigation and deployment tooling are updated for the stable version. Source archives select explicit project paths so local dependency folders cannot contribute their README, license or test files. Unmerged SAR playback-cache work in PR #140 is outside this release.

Before promotion, the candidate passed Python 3.11 and 3.13 installed-wheel checks, JavaScript and browser checks, package installation and offline viewer checks, notebook checks and paired speed gates. The [candidate PR](https://github.com/chronozarr/chronozarr/pull/141) records the exact validation and its limits. Earlier results in the [measurement log](evidence.md) retain their dated sources and environments.

Final local qualification is recorded in `release-checks.md`, `manifest.json` and `SHA256SUMS`, which identify the source commit, artifact hashes and checks. This is the prepared release record. The `v0.5.1` tag triggers trusted publishing to PyPI and npm; the website and hosted viewer are deployed separately after both registry versions are verified. Local artifacts do not establish registry publication or site deployment.
