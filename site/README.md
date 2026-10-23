# chronozarr.org

Vocs 1.4.1 + React + MDX, matching the dgov docs framework. Targeted overrides
pin patched DOMPurify 3.4.17, KaTeX 0.18.2 and TOML 5.0.1, and keep Vite on the
patched 7.3.7 and 8.3.4 branches used by the framework. These versions passed
the seven-day release-age limit and a full `npm audit` on 2026-10-23.

`mermaid-isomorphic` renders Mermaid's classic browser bundle, which embeds
old DOMPurify and KaTeX independently of the installed packages. The
`prepare-mermaid` script rebuilds Mermaid 11's public core module with the
patched dependencies before install completes and before dev/build. It checks
every installed renderer's entry paths, the actual bundled dependency inputs
and versions, and fails if that contract changes. The generated file is inside
`node_modules`; the lockfile and preparation script reproduce it. No runtime
viewer dependencies or rendering features are removed.

## Develop and deploy

```sh
(cd js && npm ci --min-release-age=7)   # esbuild, for the viewer that the build bundles
cd site
npm ci --min-release-age=7
npm audit
npx --no-install playwright install chromium
npm run test:dependencies
npm run dev
npm run build
npm run preview
npm run deploy
```

Deployment uses the repository's pinned Wrangler binary. Install root tooling
with `npm ci --min-release-age=7` from the repository root if it is absent. The site has its own
Worker, `chronozarr-docs`; the browser demo is built (`js/support/build-viewer.mjs`, esbuild, so `npm ci` in `js/` first) and the shared modules are copied into this same deployment by `scripts/copy-demo.mjs`.
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

The homepage says what a store does for a scientist, with three routes in (the live demo, the
quickstart and sharing) under the introduction. The false-color plate of the Ucayali River (August
2025, pyramid level 0) links to the same view in the viewer, over a filmstrip of every August from
2016 to 2025. Below are what a store does, the three commands, the two ways to share and four
measured figures, one each for viewer speed, value correctness, ingest speed and memory. Every
figure is traced in `docs/performance.md`. The docs use Vocs navigation, code blocks, search,
light and dark themes, and mobile menus.

The repository Markdown is the source of the documentation. `npm run sync-content` copies the
documents listed in `scripts/sync-content.mjs` into `docs/pages` before dev and build, with the
route, title and description of each. The README is not published: it is the landing page on
GitHub and PyPI, and `tests/test_docs.py` checks that its quickstart commands appear in
`docs/quickstart.md`. Edit the source documents rather than the ignored generated pages. The
importer adapts relative links (and GitHub links to published documents), names a link labelled
with a file path after its page, removes the README badges and escapes literal MDX prose syntax.
It preserves code blocks. The landing page, `docs/pages/index.mdx`, is the only hand-written page.

Addresses that chronozarr.org has published keep working. `redirects.json` maps moved paths (the
Worker imports it). `legacy-anchors.json` maps moved sections: a fragment never reaches the
server, so an inline script in `vocs.config.tsx` sends `/page#old-section` on in the browser.
`published-anchors.json` lists every route and section deployed on 2026-10-12.
`scripts/check-links.mjs` runs after every build and fails it when a link on a page, a redirect,
an old anchor or a published address leads to a page or section that the build lacks.
`scripts/make-sitemap.mjs` writes `sitemap.xml`, and `docs/public/404.html` is served for unknown
paths. `scripts/check-worker.mjs` runs the Worker against stand-in assets: every redirect, and the
`Cache-Control: no-transform` that keeps Cloudflare's Web Analytics beacon off the viewer page.

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

The current Python and npm package release is 0.4.1; see
[`docs/release-0.4.1.md`](../docs/release-0.4.1.md) for its release status.

Live: https://chronozarr.org
Fallback: https://chronozarr-docs.jake-gearon.workers.dev
Cloudflare version: bbbb7642-8bf5-45a8-9800-68f5753e6c1e

The first failed upload rejected a hostname redirect in `_redirects`. The deployed
version uses the Worker entry point instead. Some recursive DNS caches initially
retained the domain's prior absence; public DNS and the browser now resolve it.
