# Review: should chronozarr stay its own convention? (2026-10-19)

The question: keep chronozarr v0.3 as its own convention, or upstream its layout into GeoZarr and the zarr-conventions and keep only the tooling. An adversary brief argued the second case as a GeoZarr maintainer would, in falsifiable claims C1 to C8, and gave the defence D1 to D8. Each claim below is judged against a measurement, not against the argument.

Measurements, all 2026-10-18 and 2026-10-19 on one Apple M3 Max with other work running (load average 5 to 35 on 16 cores):

- **Strip and reverse tests.** About 41 variants of a synthetic v0.3 store with chronozarr metadata removed or changed, read by `chronozarr validate`, the Python and JavaScript readers, the viewer and plain xarray. Stores written by ndpyramid 0.4.0 and an ESA EOPF Sentinel-2 Zarr product, opened the same way. The 237 MUST words of the spec, sorted by what they protect.
- **Head to head.** zarr-layer 0.11.0 (2026-10-02) on maplibre-gl 6.10.0 against the chronozarr viewer and chronozarr's MapLibre layer, on the same local and public stores, a new browser per run.
- **Native CRS.** Plain GeoZarr stores in Daymet Lambert conformal conic and MODIS sinusoidal (`proj:wkt2`, `proj:projjson`, no EPSG code) and ERA5 with x from 165 to 195, opened in zarr-layer 0.11.0, GDAL 3.13.3 and xarray with rioxarray 0.23.0. Placement was checked pixel by pixel against pyproj.
- **The newcomer baseline** in [evidence.md](../docs/evidence.md#newcomer-baseline-2026-10-19).

Scripts, pages and raw results are in `data/review-2026-10/` (not in git).

## What the measurements show

**Values never depend on chronozarr.** Every variant read back byte-exact through plain Zarr and xarray, including stores with the whole `chronozarr` block removed. Four additions change what the numbers mean: per-band scale and offset, the validity rule (nodata or mask), the meaning of overview levels (valid-excluded block mean) and the version guard against v0.2 stores.

**Generic readers lose that meaning.** zarr-layer 0.11.0 does not read the band scale and offset from the `chronozarr` block, ignores the `mask` array and treats the array `fill_value` (0) as nodata: in a test store it hid valid zeros and drew masked pixels. Plain xarray returns stored values with neither scale nor mask. Two of the four public stores (Sentinel-1 and NISAR) keep their dB scale only in that block, and three rely on a mask with `nodata: null`.

**Interoperability runs one way.** zarr-layer and GDAL read chronozarr stores. Every chronozarr reader refuses every store written by others at its first check, "missing chronozarr": ndpyramid output, the EOPF product and a plain GeoZarr store alike. Many refusals guard metadata that the reader does not need to decode: a CRS written as `proj:wkt2`, a `resampling_method` other than `average`, registration URLs other than the literal v0.1 ones, a missing `_ARRAY_DIMENSIONS`.

**The layout rules buy the JavaScript reader its speed.** One chunk per timestep and cell holding all bands, square chunks of one size at every level and one codec chain are what let a timestep be one request per cell. Chunks spanning six timesteps cost 5.7 times the bytes for the first cell; one chunk per band costs three requests instead of one. The Python reader only enforces these rules; it reads the other layouts exactly.

**Sorted by what they protect,** 228 MUST sentences give: values 43, browser reader 72, interoperability policy 84, publishing and append 29. Removing the `levels` and `band_names` mirrors, level-group attributes, array aliases, coordinate arrays, provenance or volatility leaves every chronozarr reader working with the same values, though `validate` reports them. Consolidated metadata does matter: 1 request at open instead of 10.

**A CRS without an EPSG code renders.** zarr-layer 0.11.0 and GDAL 3.13.3 placed the Lambert conformal conic store, its PROJJSON copy and the sinusoidal store exactly (27 of 27 pixels each in zarr-layer; the expected column, row and value at 3 points each in GDAL). xarray with rioxarray reads no `proj:` CRS at all, EPSG included. zarr-layer drew the ERA5 store across 180° without a seam, and GDAL placed it.

**zarr-layer matches the viewer on a fast link.** It used the pyramid, reprojected native UTM on the GPU with exact values, uploaded uint16 natively, ran true colour and NDVI through a custom shader and returned 117 of 117 pixel values equal to zarr-python. It took 89 lines of page code to reach parity on time selection, scrubbing, band maths and the pixel series, against 33 for chronozarr's MapLibre layer. Warm switches were one display frame in both, with no request.

**The viewer is ahead on a constrained link and on meaning.** With the link limited to 12 MB/s and 30 ms, the viewer drew 240 of 240 steps of a 60 s scrub by dropping to coarser levels; zarr-layer drew 45 and chronozarr's MapLibre layer 128. A new step took 12 to 15 ms in the viewer against 22 to 77 ms in zarr-layer locally and 66 ms on the public store. The viewer applies the mask, the band scale and dates, and picks levels by device pixels. It also costs more: 187 to 226 MB fetched in the 1.5 s after the first frame, and 1,450 MB for a 60 s scrub where zarr-layer used 544 MB.

## Claims

| Claim | Verdict | Evidence |
|---|---|---|
| C1. A separate convention leaves generic readers without the path to correct values | Holds | zarr-layer and xarray miss the band scale and the mask; two public stores show raw integers there |
| C2. Most additions are upstream already or duplicate something that is | Partly | Mirrors and aliases are unused by chronozarr's readers; the band scale, validity and overview meaning have only proposals upstream |
| C3. The new parts fit a few small conventions | Not tested | No schema was written. 43 MUST sentences concern values: the candidate content |
| C4. The restrictions hurt users, and GeoZarr would not impose them | Holds | 6 of 20 corpus datasets have no EPSG code; the 5 that reached a store did so only by warping; zarr-layer and GDAL place such stores natively |
| C5. The browser reader is a user-experience lead that zarr-layer could reach | Partly | Parity on a fast link with 89 lines; not on a constrained link, nor for mask, scale and dates |
| C6. Maintenance and adoption favour a community effort | Holds as fact | One author, a spec first written 2026-09-29, no outside adopter ([evidence](../docs/evidence.md#adoption)). Says nothing about quality |
| C7. format-comparison.md is out of date | Holds | zarr-layer 0.11 has 16-bit textures, custom shaders and time queries; topozarr and titiler-multidim are missing; "any Zarr dtype" is too broad |
| C8. Packaging is unique, capability and layout are not | Holds | zarr-layer reads the same store; the difference is client behaviour and tooling |
| D1. Playback is the product and upstream lacks it | Holds on slow links | 240 of 240 steps against 45; on a fast link all three readers drew 239 or 240 |
| D2. GeoZarr and zarr-layer are unfinished | Not measured | From the brief's sources: the GeoZarr release candidate and the time and band issues were open on 2026-10-18 |
| D3. The guarantees cannot be checked upstream | Not tested | No upstream validator was run |
| D4. A strict subset causes no fragmentation | Fails | The bytes read everywhere; the meaning does not (C1) |
| D5. Relaxing the EPSG rule would break the main browser reader | Fails | zarr-layer places Lambert conformal conic, sinusoidal and PROJJSON exactly |
| D6. Per-band scale has no home in Zarr | Holds | No generic reader applies it; an argument to register it, not to keep it private |
| D7. The chunk layout is a measured guarantee | Holds | One request per timestep and cell; 5.7 times the bytes with time-spanning chunks |
| D8. The tooling is validated apart from the layout | Holds | The quickstart passes on three systems; 11 of 11 simulated newcomers reached a link |

## Recommendation

Neither option as stated. Keeping v0.3 as it is leaves the meaning of the values private (C1), closes the readers to every store written elsewhere and keeps a CRS rule that blocks real data with no reader needing it (C4, D5). Dropping the convention for zarr-layer gives up what is measured to be chronozarr's own: playback on slow links and correct handling of mask, scale and dates (D1, D6, D7), and the tooling (D8).

Recommended: keep the tooling, make the convention smaller and open, and take the meaning upstream.

1. **Accept any CRS the proj convention allows.** Readers and the converter take `proj:wkt2` and `proj:projjson`, and writers also emit `proj:wkt2`, which the spec already recommends and GDAL 3.12 needs. North-up stays until rotation is tested. This removes the top blocker of the baseline without warping.
2. **Open the readers.** Separate "decodes" from "conforms": the readers open any multiscales, proj and spatial store whose data they can decode, with a slower path for other chunk layouts, and `validate` stays strict. The layout rules become what chronozarr writes, measured and documented, not a gate for reading.
3. **Cut what nothing reads.** Drop the MUST rules for mirrors, aliases and registration spellings that the strip tests show no reader needs, keeping consolidated metadata and `_CRS` while GDAL 3.12 is in use.
4. **Take the meaning upstream.** Propose per-band scale, offset and units, and the validity rule (mask pairing and precedence), as small zarr-conventions with JSON schemas, and name the overview averaging. Until they are adopted, the chronozarr block keeps carrying them.
5. **Keep the viewer, and lower what it downloads.** Its slow-link playback is the measured lead; its speculative fetching after the first frame is the measured cost.
6. **Correct format-comparison.md** against current upstream, with dates.

Items 1, 2, 3, 5 and 6 are changes in this repository and can join the fix order of the baseline. Item 4 is public work with the GeoZarr working group and the zarr-conventions maintainers, under the owner's name.

## Not established

- Rotated or south-up grids in any reader.
- Level-1 placement of non-EPSG stores (checked at level 0 only).
- Whether a reader written from the spec alone by someone else would agree with chronozarr's.
- GDAL attaches no overviews when multiscales layout entries carry `spatial:shape`; chronozarr does not write it today.

## Sources

Read 2026-10-18: [geozarr-spec](https://github.com/zarr-developers/geozarr-spec) and its [roadmap](https://geozarr.org/roadmap); the [zarr-conventions](https://github.com/zarr-conventions) multiscales, proj, spatial, missing_value, CF and dependent-arrays repositories; [zarr-layer](https://github.com/carbonplan/zarr-layer) 0.11.0 and [topozarr](https://github.com/carbonplan/topozarr); [titiler-multidim](https://github.com/developmentseed/titiler-multidim); [deck.gl-raster](https://github.com/developmentseed/deck.gl-raster); the [EOPF data model](https://github.com/EOPF-Explorer/data-model).
