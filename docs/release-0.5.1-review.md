# 0.5.1 documentation and examples review

Review date: 2026-10-23. This records release preparation, not package publication or deployment. The release target is Python and npm 0.5.1. The original unpublished 0.5.0rc1 record and dated measurements retain their historical scope.

## Coverage

Two separate read-only reviews examined the root README, every top-level document in `docs/`, both JavaScript READMEs, the repository agent skill, and the site's landing page, navigation, content importer, redirects, Worker and build checks. Claims were traced to CLI options, dependency extras, converter and writer behavior, Python and JavaScript readers, and existing tests.

The examples review covered all eleven active example READMEs, 32 tracked Python scripts, six tracked notebooks, ten JavaScript example scripts, the PNG conversion shell script and the embedding example. It checked syntax and entrypoints throughout, with detailed data-flow review of ingestion, conversion, source auditing, bundling and notebook helpers. Relevant recorded manifests and results were consulted; historical outputs were not rewritten.

The SAR fixes received a second read-only review after implementation. That review independently ran their 33 focused tests and checked deterministic scene selection over 120 permutations of a tied input.

## Findings and resolution

| Finding | Resolution |
|---|---|
| NISAR and Sentinel-1 could reuse cached pixels for a different, same-sized area and encode them with a new transform | Cache sidecars now record CRS, transform and dimensions. Unverified or mismatched caches stop before extraction or overwrite. Tests cover translated grids, CRS/dimension changes, legacy sidecars, ordinary misses, changed Sentinel-1 slices, JSON round trips and moved cache directories |
| NISAR's automatic series ranking counted reprocessed versions as extra acquisition dates | Rank distinct acquisition times, then keep the highest counter for each time. Pinning and tie ordering are retained |
| Water-mask instructions could subtract Sentinel-2's baseline offset a second time | Restrict the legacy correction flag to separately verified offset-bearing mosaics; current ingest already corrects each source scene |
| Generic source-audit instructions invoked helpers requiring an explicit validity mask | Scope those helpers to the masked Sentinel-2 sample and create both audit stores from the same manifest |
| NISAR setup omitted the required credential file and the recorded command drifted over time | Document netrc setup and authenticated dry-run metadata reads; pin the recorded date range, track, direction, frame and six-worker run; describe the missing cycle |
| Signed embedding links, `serve_store` inputs and masked stored values were described incorrectly | Explain wordmark query redaction, distinguish local-only serving from remote viewing, and require masks when interpreting stored values |
| Overview coverage was presented as an exact observation count, and the public imagery store was said to have a coverage plane | Distinguish level-0 counts from rounded overview summaries; record the current public store's absent mask/coverage metadata |
| Minor reproduction and navigation errors | Correct the Sentinel-2 doctor path, reject the unsupported SWOT intensity live-check mode, qualify old missing-chunk browser errors, narrow v0.3 compatibility wording and remove the candidate viewer sidebar label |
| Release versions, distribution instructions and package landing-page links were stale | Align current examples and package pins to 0.5.1, document the npm viewer bundle, and use canonical release/development links in npm and PyPI-facing text |
| A source build from a populated checkout included nested dependency README, license and test files | Select explicit root paths for the sdist and inspect the resulting archives |
| Deployment tooling retained four npm audit findings | Pin Wrangler 4.151.1 within the unchanged seven-day policy and audit the root tooling in site CI. Root, JavaScript and site audits report zero findings |

## Validation and limits

The combined documentation, release-version and example checks passed 120 tests. Ruff lint/format, type checks and JavaScript syntax checks passed. The site build checks internal links, published routes, redirects, anchors, viewer assets and Worker behavior. Final artifact hashes and installation checks are recorded with the release preparation artifacts and pull request.

The reviews did not rerun full remote ingests, upload datasets, validate new cloud-account integrations or reproduce every historical performance measurement. Legacy SAR caches without spatial identity now require a fresh cache name or directory; no existing cache was rewritten during this work. NISAR discovers scenes and reads the first granule's grid metadata before it can compare the requested window with cached metadata.

The packaged Python and JavaScript runtime remains the reviewed candidate's runtime. The SAR fixes are repository examples, distributed separately from the Python wheel and npm viewer. The dataset host, package registries and hosted site remain separate publication steps.
