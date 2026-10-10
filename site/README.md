# chronozarr.org

Vocs 1.4.1 + React + MDX, matching the dgov docs framework. The `toml` override
pins the patched parser at 4.2.0; `npm audit` reports zero vulnerabilities.

## Develop and deploy

```sh
cd site
npm ci
npm run dev
npm run build
npm run preview
npm run deploy
```

Deployment uses the repository's pinned Wrangler binary. Install root tooling
with `npm ci` from the repository root if it is absent. The site has its own
Worker, `chronozarr-docs`; the browser demo and its shared modules are copied into this same deployment by `scripts/copy-demo.mjs`.
Build output is `docs/dist`. `worker.js` redirects www to the apex, redirects
the paths of removed pages to their replacements, and forwards other requests to
static assets. Unknown paths return 404.

## Content and visual direction

The look is a 1960s to 70s corporate annual report: Archivo (a Univers-like grotesk)
with IBM Plex Mono for labels and code, cool neutrals, one steel-blue accent, hairline
rules, no rounded corners. Every colour, face and radius is a Vocs theme variable in
`vocs.config.tsx`; `docs/styles.css` carries the landing page and the few Vocs rules the
variables cannot express (letter-spaced caps for sidebar groups, table rules, callouts
without fills). Those rules target Vocs's generated class names, which a Vocs release can
rename; `scripts/check-vocs-classes.mjs` runs after every build and fails it when a name
in `styles.css` is absent from the built CSS. The fonts load from Google Fonts with system
fallbacks.

The homepage opens with a false-color plate of the Ucayali River (August 2025, pyramid
level 0) over a filmstrip of every August from 2016 to 2025, then four measured figures
and three routes into the documentation. The docs use Vocs navigation, code blocks,
search, light/dark themes, and mobile menus. The format specification is labeled v0.3 Draft.

The repository Markdown is the source of the documentation. `npm run sync-content`
copies the documents listed in `scripts/sync-content.mjs` into `docs/pages` before
dev and build. The root `README.md` becomes `/getting-started`, and `../docs`,
`../js`, `../spec` and `../examples/bring_your_data` supply the other pages.
Edit those source documents rather than the ignored generated pages. The importer
adapts relative links, removes the README badges and escapes literal MDX prose
syntax. It preserves code blocks. The landing page, `docs/pages/index.mdx`, is the
only hand-written page. Keep its introduction equal to the opening of the README.

The homepage images are WebP: the plate at 2759 px (about 970 KiB, served to 2x screens
through `srcset`) and at 1380 px (about 300 KiB), plus ten 260 px thumbnails of about
10 KiB each. The page does not load the viewer or fetch raster chunks.
`scripts/make-previews.py` records their source, dates, band order, levels and fixed
display stretch. Rebuild them from the local demo store with
`uv run --with pillow python site/scripts/make-previews.py` at repo root. They are
false-color illustrations (near infrared, red, green), not numeric exports.

## Verification and live deployment

Verified 2026-10-02 (America/New_York):

- Production build and dependency audit pass.
- All 18 routes return 200 locally with one h1, no page errors, and no horizontal
  overflow at 390 px; homepage reviewed at desktop and mobile sizes.
- Internal page/file links and fragment anchors pass.
- Synthetic quickstart roundtrip, validator, xarray backend, and plain Zarr read pass.
- Live homepage and docs load; search returns specification results (historical v0.2 deployment check).
- Unknown live paths return 404; www returns 301 to the apex while preserving path.
- The package versions recorded by this historical deployment check were PyPI and npm chronozarr 0.2.1.

The current Python and npm package release is 0.4.0; see
[`docs/release-0.4.0.md`](../docs/release-0.4.0.md) for its release status.

Live: https://chronozarr.org
Fallback: https://chronozarr-docs.jake-gearon.workers.dev
Cloudflare version: bbbb7642-8bf5-45a8-9800-68f5753e6c1e

The first failed upload rejected a hostname redirect in `_redirects`. The deployed
version uses the Worker entry point instead. Some recursive DNS caches initially
retained the domain's prior absence; public DNS and the browser now resolve it.
