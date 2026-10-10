# chronozarr 0.4.0

This release makes it easier to turn an existing raster series into an interactive
viewer and share it with a colleague. Python and npm package versions advance to
0.4.0; the store format remains chronozarr v0.3. Existing v0.3 stores need no conversion.

## Changes since 0.3.1

- Convert a directory, quoted glob or S3 prefix of dated GeoTIFFs, with a dry run
  that reports incompatible inputs before writing. Preserve band metadata and nodata.
- Preview local stores from the command line and use documented remote notebook routes.
- Share a local store through a temporary Cloudflare quick tunnel. The command
  reports startup stages, checks browser access before printing the viewer link,
  and measures a bounded cell read for sharded stores.
- Copy the current viewer link, including the selected catalog store, timestep,
  product, display limits and map view. Playback and loop settings are not included.
- Open a store URL in the viewer, with clearer errors and redacted credential-bearing
  URLs in messages.
- Inspect and assign band roles, and build checked initial-view links.
- Publish stores to S3-compatible storage, GCS or Azure, and update eligible appended
  stores at the same address. Cloud adapters have focused tests; live cloud-account
  behavior still needs validation in the user's environment.
- Improve viewer scrubbing, mobile controls and accessibility, and provide public-data
  examples and a raster-to-colleague walkthrough.

## Colleague trial

Follow [the walkthrough](python.md#share-a-store-through-a-tunnel) with a small,
non-sensitive series first. The sender needs Python 3.11+, `chronozarr[geo]` and
`cloudflared`; the recipient needs a modern browser. Inputs must be dated and share
the same north-up grid and band metadata. Keep the sender's laptop awake and the
sharing command running. Anyone with the link can read the store while it runs.
The temporary link uses the sender's upload bandwidth.

Ask the recipient to open the copied link, confirm the intended dataset and view,
scrub to another date, and inspect a pixel. Record any confusing step and the time
until the first useful view. This independent colleague trial remains to be done.

## Release status

The release commit is `e982518b39edef5447e70e641a441f633b498686`, tagged
`v0.4.0`. The Python package on PyPI and the JavaScript package on npm both
report version 0.4.0. The package versions advance independently of the store
format: this release reads and writes chronozarr v0.3 stores.

The release checks covered the Python, JavaScript, browser and notebook suites,
the Python wheel and sdist, and the npm tarball. The installed packages expose
the new CLI and viewer features.

The independent colleague trial remains to be done. Live behavior of the cloud
publishing adapters still needs validation in the user's cloud accounts. The
hosted viewer's deployed Copy link flow also needs a fresh production
verification; package publication and the hosted viewer are separate.

The synthetic live tunnel test passed during review, including public byte ranges,
CORS, exact decoded values and shutdown. It does not replace the colleague trial
or guarantee that a fresh tunnel hostname becomes reachable within the 90-second
wait deadline.
