# Changes on main since 0.4.0

The Python and npm packages on PyPI and npm are 0.4.0. The changes below are on the main branch and reach users as the table says. The store format stays v0.3.0: no change here alters a store.

## Where each change reaches users

| Change | Python release | npm release | Website deploy | Repository only |
|---|---|---|---|---|
| Encode unsharded stores in time slabs: encode memory nearly flat in the number of timesteps (PR #99) | Yes | | | |
| `share` prints the address at once, waits 15 s before the first lookup (link after about 30 s instead of 90 s), checks browser access with one decoded cell per level, stops when cloudflared exits, and says that the link needs this computer on and online | Yes | | | |
| `preview` and `share` print no traceback when the viewer cancels a read | Yes | | | |
| Missing-extra messages name the `uv tool install`, `uv add` and pip commands; CLI messages name chronozarr.org pages instead of repository files | Yes | | | |
| `publish` gives public-access advice only after HTTP 401 or 403; S3 credential and upload errors name the fix | Yes | | | |
| `link` says on stderr that browser access (CORS) was not checked; a local path points to `preview` and `share` | Yes | | | |
| `convert` names a folder of GeoTIFFs `dated GeoTIFFs`; a month name in a file name points to a manifest; `bands` prints product ids | Yes | | | |
| PyPI summary and Documentation link; the README as the PyPI description | Yes | | | |
| Viewer: a failed `?store=` link keeps its error instead of showing the demo; messages for a blocked local store and a stopped share link | | Yes | Yes | |
| Viewer: a bare visit to the demo starts at the latest well-covered timestep | | Yes | Yes | |
| Viewer: Copy link says when a link opens only on this computer; the wordmark links to chronozarr.org | | Yes | Yes | |
| npm homepage and description | | Yes | | |
| Documentation reorganized, new homepage, redirects for old addresses, sitemap, 404 page, no "Ask in ChatGPT" button | | | Yes | |
| No Cloudflare Web Analytics on the viewer page (`Cache-Control: no-transform` from the Worker) | | | Yes | |
| Sentinel-2 example: adaptive downloads, memory budget, spatial strips, coverage counts (PRs #90, #95, #97), `--bbox` and `--aoi-file` (PR #100) | | | | Yes |

The website and the hosted viewer at chronozarr.org/demo/ deploy together: `npm run deploy` in `site/` copies `js/demo` into the site. A viewer folder copied with `chronozarr-viewer` from npm gets the viewer changes only with an npm release.

## Order

1. Merge PR #100.
2. Deploy the website from a detached worktree at `origin/main`. The pages describe 0.4.0 where it differs, so this does not wait for a release.
3. Release 0.4.1: one `v0.4.1` tag publishes both packages. Before the tag, set the version in `pyproject.toml` and `js/package.json`, move this page into `release-0.4.1.md`, update the install pins in the docs (`tests/test_docs.py` checks them), and replace the 0.4.0 sample output in `docs/share.md` and `docs/quickstart.md`.
4. Deploy the website again, so the pages and pins describe 0.4.1.
