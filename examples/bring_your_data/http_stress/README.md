# HTTP snapshots, cache pressure and interrupted reads

These checks extend the twelve-date adoption sample without modifying its inputs.
The server publishes completed files by atomic replacement and root metadata last.
This represents an object host replacing completed uploads; it does not establish
snapshot safety while individual objects are partially overwritten or metadata
is cached inconsistently by a CDN.

From the repository root:

```bash
uv run pytest -q tests/test_http_append_snapshot.py
uv run --extra geo python examples/bring_your_data/http_stress/verify.py \
  data/adoption/extended-20261003/input data/adoption/http-stress-20261003
cd js
npx playwright test e2e/adoption-http-stress.spec.js e2e/reader-http.spec.js \
  e2e/viewer-lifecycle.spec.js --workers=1 --reporter=json \
  > ../data/adoption/http-stress-20261003/browser-report.json
```

The Python regression opens the HTTP reader and lazy xarray backend before an
11-to-12-date append. No raster frame is read before publication. Every old frame
then matches the source, the old handles retain eleven dates and reject index 11,
and reopened handles see all twelve. Both unsharded and four-date sharded layouts
are exercised on controlled synthetic inputs. The real-input harness checks all
values and explicit masks of the existing twelve-date acquisition sample, using
new output directories exclusively.

Browser checks cover the same old/new axes with every valid cell pixel checked
for every date, plus a return to date zero. An 8,192-byte reader chunk-cache budget
forces eviction; this is not a browser-process or GPU memory limit. First-request
HTTP 500 failures are injected for raster objects, including explicit background
prefetch and subsequent demand reads, and successful background/demand chunk
probes and retry counts are recorded. Closing both handles must release all chunk
cache bytes. Existing `reader-http.spec.js` covers body truncation, transient HTTP
failures, cancellation reaching a real socket and subsequent reader recovery.
Existing viewer lifecycle checks cover repeated store replacement and GPU handles.

The visible-frame test injects transient HTTP 500 failures and staggered responses
while scrubbing twelve distinct synthetic color frames. It records paint events
and samples the actual framebuffer at all nine cell centers. A complete requested
frame eventually arriving is a separate condition from visible frames being whole:
`renderNow().complete` can be false while a whole previous/coarser frame remains
visible. The evidence checks every recorded paint's partial flag, cell counts and
sampled framebuffer colors. Nine sampled points are not exhaustive screen-pixel
coverage or observation of every compositor refresh.

Run evidence is attached to the Playwright JSON report as base64 JSON bodies.
The list reporter prints pass/fail but does not retain successful inline attachments. Local checked-in results
will record the executed commands and outcomes; no CDN, independent-user or
read-path speed improvement is claimed. Production reader code is unchanged.

Executed 2026-10-03: two Python regressions and five browser checks passed.
The real twelve-date snapshot check passed all values and masks. See `results/`.

The retained run recorded 100 full-cell comparisons per reader-layout test
(11 old dates, 12 reopened dates, return to date zero, four cells each), two
successful background chunks and 62 successful demand chunks. Both layouts
reached the configured 8,192-byte chunk-cache cap and returned zero cache bytes
on close. First-request failure counts were 48 unsharded and 12 sharded objects.
The visible-frame run injected 114 first-request failures; 21 objects were
retried before the final frame completed. Superseded speculative requests can be
cancelled, so injection counts are not counts of successfully retried objects.

The retained visible-frame run recorded thirteen paints, zero partial flags and
matching colors at all nine cell centers, ending at timestep eleven.

Final integration rerun: all 44 browser tests passed, with zero skips, failures
or flaky tests (53.69 seconds). The full-suite command was:

```bash
cd js
npx playwright test --workers=1 --reporter=json > /tmp/chronozarr-round2-browser.json
```

The three stress attachments from that run are retained separately as
`results/full-suite-*.json`. Its delay schedule hashes only the URL pathname,
so random local server ports do not affect response ordering. `run-context.json`
records final source hashes separately from the earlier five-target run hashes;
the earlier evidence is historical and retained unchanged. Suite durations are
correctness-test execution times, not read-path performance measurements.
