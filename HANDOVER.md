# Handover: make chronozarr independently adoptable

Date: 2026-10-02 (America/New_York)
Branch: main. Starting commit: 82a4001 (viewer migration).
Read `.napkin.md` first, then inspect current git state. The user-provided AGENTS instructions
now describe chronozarr, js/demo/, static hosting, and unchanged read-path speed gates.

## Objective and boundaries

Jake wants useful open-source infrastructure, not a business, SaaS, pricing, or an auth service.
Target: publish numeric raster time series once and use the same data in Python, a browser
viewer, and another application's MapLibre layer or iframe. Do not present a successful local
example as independent adoption, continental validation, or a universal performance advantage.
Local project-directory rename remains explicitly deferred. Existing data hostname/bucket
retain legacy names. Preserve untracked .wrangler/ and notebook checkpoint directories.

## Completed

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

1. Reproduce adoption from a clean checkout/environment. Follow the documented recipe literally,
   without existing data or developer-installed dependencies. Record time, missing steps and
   errors. Use a new output directory. Do not change global quarantine settings. A fresh agent
   is useful for detecting assumptions, but is not an independent outside user.
2. Make a fair browser-delivery comparison on the same prepared real data: chronozarr,
   ordinary Zarr through its native reader, and COG-per-date. Match bands, dtype, masks,
   viewport, resolution, dates and image treatment. Compare equivalent overview values or
   pin level 0. Interleave runs; separate cold opening, warm stepping, scrubbing, decoding,
   requests, transferred bodies/wire bytes, and peak memory. Publish limitations. Existing
   bench/ helpers are reusable but hardcoded to older stores; do not silently reuse their results.
3. Extend only after baseline: 12-24 dates and a larger footprint, then an append and reader
   reopening. Measure bounded memory and incomplete-frame behavior. Avoid a continental
   ingest before proving these paths. Do not change unsharded default without measurements.
4. Resolve integration friction found in those tests. The full self-hosted viewer currently
   needs a checkout; npm includes reader/layer and select renderer dependencies, not a turnkey
   complete viewer distribution. Investigate a documented distributable viewer entry point.
   Keep the demo as a reference application and the libraries usable independently.
5. Obtain outside-user feedback with the reference recipe. Do not contact anyone on Jake's
   behalf without explicit authorization. Private-data integration and multi-store orchestration
   should follow a demonstrated need, not an enterprise checklist.

Before reader changes, measure the existing read-path gates, make one change at a time and
measure again. Do not optimize the Python reader solely from this tiny local sample or claim
native Zarr is universally fastest. No push, release, or local directory rename requested
as part of this handover.
