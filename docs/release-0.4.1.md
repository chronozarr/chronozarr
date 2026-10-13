# chronozarr 0.4.1

A patch release of the Python and npm packages. It lowers encode memory, makes sharing faster and clearer, and fixes messages and viewer behavior found in a newcomer trial. The store format remains chronozarr v0.3: no change alters a store, and v0.3 stores need no conversion.

## Python package

- Encoding an unsharded store works in time slabs, so its memory stays nearly flat as the number of timesteps grows. For 117 Ucayali months the encode step peaked at 1.34 GB ([memory](performance.md#encoder-time-slabs)).
- `share` prints the public address at once and waits 15 s before looking it up, so a cached failed lookup no longer delays the link: about 30 s to the link instead of about 90 s ([measurement](evidence.md#time-to-a-link-2026-10-12)). It checks browser access with one decoded cell per level, stops when cloudflared exits, and says that the link needs the sharing computer on and online.
- `preview` and `share` print no traceback when the viewer cancels a read.
- A missing optional dependency names the install command for `uv tool install`, `uv add` and pip. Messages name chronozarr.org pages instead of repository files.
- `publish` gives public-access advice only after HTTP 401 or 403. Missing S3 credentials and refused uploads name the fix.
- `link` says that browser access (CORS) was not checked, and points a local path to `preview` and `share`.
- `convert` names a folder of GeoTIFFs `dated GeoTIFFs` in its plan; a month name in a file name points to a manifest. `bands` prints the product ids that `link --product` takes, so its output has a new first column.
- The PyPI summary and links point to chronozarr.org.

## npm package

The reader and the MapLibre layer are unchanged. The viewer that `chronozarr-viewer` copies:

- keeps the error on screen when a `?store=` link fails, and never shows the demo store in its place, with messages for a blocked local store and a stopped share link;
- starts a bare visit to its catalog at the latest well-covered timestep, while links keep their timestep;
- says when a copied link opens only on this computer, and links its wordmark to chronozarr.org.

## Website

The documentation, the homepage and the hosted viewer at chronozarr.org were deployed on 2026-10-13, before this release. The viewer page there runs no analytics.

## Not in the packages

The Sentinel-2 example in `examples/sentinel2_pc` gained adaptive downloads, a memory budget, spatial strips, coverage counts, and `--bbox` and `--aoi-file`. It runs from a checkout of the repository.

## Colleague trial

The independent trial with a person who has not used chronozarr remains to be done. [Colleague trial](colleague-trial.md) is the protocol.

## Release status

Not yet released. When the `v0.4.1` tag has published both packages, record here the release commit, the tag and the versions that PyPI and npm report.
