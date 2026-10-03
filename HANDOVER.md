# Handover: make chronozarr independently adoptable

Date: 2026-10-03 (America/New_York)
Branch: main. Starting commit: 82a4001 (viewer migration).
First adoption round committed locally as `139a6cd` on 2026-10-03; the second
validation round follows that checkpoint. Neither round was pushed or deployed.
Read `.napkin.md` first, then inspect current git state. The user-provided AGENTS instructions
now describe chronozarr, js/demo/, static hosting, and unchanged read-path speed gates.

## Objective and boundaries

Jake wants useful open-source infrastructure, not a business, SaaS, pricing, or an auth service.
Target: publish numeric raster time series once and use the same data in Python, a browser
viewer, and another application's MapLibre layer or iframe. Do not present a successful local
example as independent adoption, continental validation, or a universal performance advantage.
Local checkout renamed to `/Users/jakegearon/projects/chronozarr` on 2026-10-03. Existing data hostname/bucket
retain legacy names. Preserve untracked .wrangler/ and notebook checkpoint directories.

## Completed

- 2026-10-03: Reconciled current demo documentation with the catalog's unsharded
  `chronozarr-4` imagery revision; `chronozarr-3` is the historical sharded revision of
  the same dataset, not a package/spec version. Historical comparison URLs are now preserved
  when source guides are synchronized to the website. Corrected the embed example CRS to
  EPSG:32718 from the local imagery metadata.
- Added complete npm viewer assets and `chronozarr-viewer OUTPUT [--store URL]`.
  An independently installed local tarball passed rendering, timestep, inspector and embed
  checks with zero external requests/browser errors. Packaging guard runs on pack; Browser CI
  now includes the installed-package smoke test. This change is not released to npm.
  Guide: `docs/viewer-distribution.md`; check: `npm run test:package` from `js/`.
- Added `bench/adoption/`: fifteen rotated browser runs over the prepared three-date sample,
  all level-0 values and masks identical across chronozarr, native zarrita and geotiff.js.
  Median localhost cold reader opening plus assembly: 49.4, 45.7 and 168.1 ms respectively.
  Native-repeat cache behavior measured separately. No GPU/rendered latency, remote/CDN,
  isolated codec or peak-memory comparison is established; see the benchmark README.
- Added `examples/bring_your_data/extended/`: twelve distinct real 2020 acquisitions on
  a 227 x 186 grid, exact local/HTTP values and masks, physical scaling and append/reopen
  checks. All 22 previous data/mask chunks remained unchanged. All twelve dates eventually
  rendered completely; a 10 ms requested-frame readiness sampler recorded one not-ready
  observation in 32 samples. That does not imply a partially painted canvas. Peak sampled
  reader cache was 28.1 MB; the subsequent checks below extend the footprint and stress cases.
- Second round: `bench/rendered/` runs all three sources through the same level-0 GPU
  renderer, with exact values, masks and framebuffer hashes across eighteen sessions.
  Local cold GPU-complete medians were 78.4/77.1/181.4 ms (chronozarr/zarrita/COG).
  Page-target 50 Mbit/s, 40 ms emulation gave 1271.0/1367.1/1025.7 ms; worker descendants
  are not fully observed or confirmed throttled. Total monitored wire counts are lower
  bounds. No production/CDN, compositor-presentation or native-viewer scrub claim follows.
  Application-cached warm draws use the same retained GPU slots in all three paths.
- `examples/bring_your_data/large/`: twelve pinned real dates freshly prepared on a
  1136 x 1107 grid, forced star-delta to exercise anchors/deltas. Every browser data/mask
  hash matched Python COG goldens; local/HTTP values and physical scaling were exact.
  Append preserved 308 old chunks. Sampled summed Chromium RSS was 1.89 GB with the
  viewer and a second numeric reader active; shared pages can be double counted, and
  this is neither an isolated reader/GPU allocation nor a true peak memory bound.
- `examples/bring_your_data/http_stress/`: already-open HTTP and lazy backend snapshots
  remain eleven dates with exact uncached old frames; reopening sees twelve. Synthetic
  regressions cover sharded and unsharded layouts, atomic replacement and metadata last.
  Browser failure/eviction checks hold a tiny 8192-byte reader cache; thirteen recorded
  paints had no partial flags and the correct colors at all nine sampled cell centers.
  Full compositor or exhaustive screen-pixel observation is not established.
- Corrected the first comparison's even-sample medians using its unchanged raw timings:
  uncached steps are 23.95/24.85/139.15 ms. Cold-opening medians were unaffected.
- Combined second-round validation: 590 Python unit tests and 44 browser tests passed,
  with no skipped, failed or flaky browser tests. Ruff checks and formatting passed;
  the docs/site build passed. Production reader code was unchanged. New stress evidence
  retains the initial run and a separate final full-suite run with matching source hashes.
- Viewer migrated to js/demo/; API global and embed messages are chronozarr / chronozarr:*.
- Demo deployed at https://chronozarr.org/demo/ alongside docs; migration commit 82a4001.
- Migration checks: 584 Python passed, 4 skipped; 423 JS passed; 41 browser tests passed.
- Added examples/bring_your_data/: conversion recipe, static bundle builder, localhost preview,
  lazy Python pixel-history reader, browser integration check, independently fetched real input,
  numeric verification, and local interleaved read comparison.
- Three Sentinel-2 L2A acquisitions near Lake Mead, 905x741, four uint16 bands, EPSG:32611;
  dates 2020-05-05, 2020-06-09, 2020-07-29. Exact scene IDs in committed evidence source.json.
- Prepared COG values and masks exactly preserved at level 0; physical scaling and NaN validity
  passed. Native xarray/Zarr level-0 reads also exact.
- Self-hosted viewer and iframe passed time/product controls, pixel-click reporting, and
  playback controls with zero external requests or browser errors.
- HTTP doctor: 10 ok, 4 info, no warnings/failures. Ruff and site build passed.
- Example selection fixed: low-cloud tile initially covered only 5% of AOI. Fetcher now
  requires full raster-bound coverage and >=50% valid pixels. Final sample 98-99% valid.
- Corrected explanatory iframe path in js/examples/embed.html to demo/index.html.

## Artifacts and evidence

- examples/bring_your_data/README.md: exact adoption commands, limitations, source workflow.
- examples/bring_your_data/results/lake-mead-2020/README.md: bounded findings.
- Same results directory: source.json, verification.json, local-comparison.json,
  browser-check.json, doctor-http.txt, run-context.json (dependencies and script hashes).
- Local gitignored data: data/adoption/lake-mead-2020/{input-full,series,plain-zarr,published-final}.
- Rejected edge-only inputs remain at data/adoption/lake-mead-20201002/input. Do not use them.
- Static preview servers were stopped. No new cloud data upload or package release performed.
- New guide is wired into site navigation/content sync; it has not been deployed to the live site.
- run-context.json records the dirty checkout at measurement time; that is historical provenance,
  not a statement about the checkout state after the checkpoint commit.

## Findings that control interpretation

Auto encoding chose none: sampled star-delta/plain bytes 0.92, missing the 0.85 threshold.
Auto and explicit plain stores differ by only 116 metadata bytes. COGs use DEFLATE and
Zarr zstd 5. Overview values/masks were not reconciled, so aggregate bytes do not establish
a format or bandwidth advantage.
Five rotated interleaved repetitions reading all three level-0 frames and masks, fresh handles,
warm local filesystem caches: rasterio COG median 94.8 ms; chronozarr 57.5 ms;
native xarray/Zarr 19.6 ms. Assertions outside timing. Different Python pipelines; this is
not a browser, cold-storage, CDN, ROI-read or scale benchmark.

## Next steps, in priority order

1. Clean-checkout reproduction completed 2026-10-02 from committed `2c04bfd`, new virtual
   environment and newly downloaded inputs. See
   `examples/bring_your_data/results/clean-checkout-20261002/README.md` and adjacent evidence.
   Numeric fidelity, native Zarr, HTTP doctor, lazy HTTP pixel history and browser/embed checks
   passed. Host caches/interpreters/Chromium were reused; this is not an outside-user run or
   cache-empty computer. Default Python 3.14 required a codec source build; verified runtime
   was explicitly selected Python 3.13. No reader changes, push, release or deployment.
2. Extend the completed matched GPU-renderer experiment to a real remote/static-host
   delivery experiment when requested. Before interpreting the emulated profile as all-target
   network performance, instrument worker descendants and verify their throttling. Separate
   source-module delivery, codec-only work and compositor presentation if those are claimed.
3. Profile memory with one reader/viewer and controlled cache/GPU budgets on the larger
   dataset. The two-reader 1.89 GB summed RSS sample is a diagnostic, not a memory ceiling.
   Extend synthetic fault/eviction checks to larger real workloads and real host cache policy;
   keep publication atomic and metadata last. Avoid a continental ingest before these checks.
   Do not change unsharded default without measurements.
4. Release the newly tested npm viewer distribution when requested; until then, use the
   documented local tarball workflow. Keep the demo as a reference application and the
   libraries usable independently. No release or deployment was requested for this work.
5. Obtain outside-user feedback with the reference recipe. Do not contact anyone on Jake's
   behalf without explicit authorization. Private-data integration and multi-store orchestration
   should follow a demonstrated need, not an enterprise checklist.

Before reader changes, measure the existing read-path gates, make one change at a time and
measure again. Do not optimize the Python reader solely from this tiny local sample or claim
native Zarr is universally fastest. No push or release requested. The local directory rename was authorized and completed
on 2026-10-03; reopen the project at its new path in Codex.
