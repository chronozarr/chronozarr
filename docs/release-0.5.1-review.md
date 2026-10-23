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

The first pass retained the candidate's packaged runtime. The second pass below adds a Python
server containment fix; the JavaScript reader and renderer remain unchanged. The SAR fixes are
repository examples. The dataset host, package registries and hosted site remain separate
publication steps.

## Second independent review

Three fresh reviewers examined commit `57b9640` independently, covering documentation and
documented workflows, examples and scientific interpretation, and release archives and hosting.
They formed their findings before consulting the first review record. All six confirmed findings
were fixed, then the changed code and instructions received focused follow-up reviews.

| Severity | Finding | Resolution |
|---|---|---|
| P1 | A file or directory symlink inside a served store or viewer could expose a file outside that folder through preview or share | Resolve each requested target and require it to remain inside its own mounted root. Escaping, broken and looping links return 404. Internal links retain byte-range access and the requested filename's JavaScript/WebAssembly MIME type |
| P2 | The hosted MapLibre layer imported `/shared/` modules without CORS headers, breaking imports from external sites and the default notebook bridge | Add the shared-module header rule. A new hosting check imports reader, MapLibre and GeoLibre modules from another origin through the pinned Wrangler asset handler and Chromium; CI runs it after the site build |
| P2 | The sdist shipped tests but omitted their example, documentation, JavaScript and helper inputs; the corpus helper also required Git at import | Include the tests' source inputs through explicit root paths, exclude dependency/generated trees, and label Git-less source archives honestly. CI collects the shipped unit tests from an isolated extracted archive |
| P2 | Water-mask script docstrings still prescribed the legacy Lake Mead offset correction, contradicting the corrected README | Remove that recipe and restrict source instructions and both commands' help to verified uncorrected legacy mosaics |
| P2 | Sentinel-1 accepted partial date ranges while claiming only complete months were composited | Keep custom intervals, warn about partial boundary months during normal and dry runs, and record the query interval and partial months in store provenance. Explain that time labels remain the month's first day |
| P2 | The conversion guide recommended `encode` as a generic in-memory shortcut for NetCDF/Zarr variables, despite different dimension, suffix and CF-decoding requirements | Keep `convert` as the documented route for those variables and narrow the CLI summary of `encode` |

The independent initial checks passed 73 documentation tests, 87 example tests and 24 release
checks, with overlapping coverage. Synthetic probes reproduced the outside-file exposure,
double offset correction and partial-month labeling. Real Chromium reproduced the CORS failure
through Wrangler before the header repair. Follow-up review found and resolved the MIME-type
regression in an early containment fix, then reported no remaining actionable findings in the
repairs.

Regression checks cover GET, HEAD and byte ranges; file and directory symlinks; both mounts;
internal JavaScript/WebAssembly aliases; leap-year month boundaries; partial-range provenance;
and source archives with no Git executable or checkout. A filesystem lookup microbenchmark for
36 local objects measured a median of 0.23 ms before containment checks and 1.85 ms after them.
The release artifacts retain the detailed check results and source identity.

The second pass did not run full remote ingests, public tunnels or production deployments, and
did not qualify minimum-dependency combinations. Archive collection checks imports and inputs;
focused tests and installed-wheel checks provide separate execution coverage.
