# Self-host the viewer

**Local release candidate — 0.5.0rc1, unpublished.** This guide builds the npm `0.5.0-rc.1` viewer from the [candidate checkout](development.md). The same build is copied by the [local npm artifact](development.md#install-local-candidate-artifacts). For the released npm package, use the [0.4.1 distribution guide](https://github.com/chronozarr/chronozarr/blob/v0.4.1/docs/viewer-distribution.md).

The checkout builds the page, one script for the viewer and reader, one for the decode worker, the codecs and their licenses. The viewer runs in the browser. The host only serves files. Node.js builds and copies the files once and is not needed on the host. The npm 0.4.1 package copies a module-based viewer and does not contain this production bundle.

## Copy the viewer

1. Use the prepared [candidate checkout](development.md#install-the-command-line-tool). From its root, install the JavaScript build dependencies:

   ```sh
   npm ci --prefix js --min-release-age=7
   ```

2. Copy the viewer into a new folder, `published/`.

   ```sh
   node js/support/viewer-bundle.mjs published --store ./store
   ```

3. Copy your store into that folder.

   ```sh
   cp -R /path/to/your/store published/store
   ```

4. Upload `published/` to a static host, and open `index.html` through the host. A browser blocks the viewer's modules when the file opens from disk.

The folder works at a domain root or under a subpath. Sharded stores need a host that answers byte-range requests.

To preview the folder on your machine, use a server that answers byte-range requests. Start it inside the folder:

```sh
cd published
uv run --with rangehttpserver python -m RangeHTTPServer 8000
```

## Preview a local store with the copied viewer

`chronozarr preview` and `chronozarr.view` normally load the viewer from `https://chronozarr.org/demo/`. Pass the copied folder to serve the viewer from your own machine. No internet access is needed.

```sh
chronozarr preview my_store --viewer-dir published
```

To show a local store to a colleague for a short review, use `chronozarr share my_store` instead. It starts a temporary public link through `cloudflared` and uses the hosted viewer by default; keep the terminal and laptop running. See [Share a temporary link](share.md). A copied viewer is useful when you need an offline viewer or one served on your own origin.

In a notebook, pass `viewer_dir="published"` to `chronozarr.view` or `chronozarr.player`. The folder needs `demo/index.html`, which the copy command writes. The same server serves the viewer and the store, so there is no CORS or mixed-content step. On JupyterHub the viewer and the store go through `jupyter-server-proxy`. See [notebooks.md](notebooks.md#remote-notebooks).

## Use a store on another host

Give an absolute URL instead of a path:

```sh
node js/support/viewer-bundle.mjs published --store https://example.org/my-store
```

The host of the store must send CORS headers. See [hosting requirements](hosting-requirements.md).

## What the copy command does

- It refuses to overwrite a folder that exists.
- It builds and copies this checkout's viewer. A browser reads `demo/viewer.js` in one request instead of fetching the source module graph. The WebAssembly codecs (`demo/codecs/`) and proj4 (`demo/chunks/`) load only when a store needs them. The copy command builds the viewer first using the dependencies installed above.
- It does not check or copy data.
- It writes an empty catalog. A store that fails to load never falls back to the public sample data.

Without `--store`, the landing page shows "No store selected". To choose a store, open `demo/index.html?store=<URL>` in the copied folder. For an iframe, add `&embed=1`. See [embedding](embedding.md) for the controls and the origin restriction.

## Test the package

Run `npm run test:package` from `js/`. The test packs the package and installs it in a new temporary folder. It checks that the copy command refuses to overwrite. It then renders a synthetic store from the copied viewer under a URL subpath. It checks the time controls, the pixel inspector and embedded rendering. It also checks that the page makes no outside requests and logs no browser errors.
The test uses the development Playwright installation.
