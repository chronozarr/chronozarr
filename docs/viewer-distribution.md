# Self-host the viewer

The npm package contains the full viewer: the reader, the web workers, the codecs, the GIF encoder and the static assets. The viewer runs in the browser. The host only serves files. Node.js copies the files once and is not needed on the host.

## Copy the viewer

1. Install the package.

   ```sh
   npm install chronozarr
   ```

2. Copy the viewer into a new folder, `published/`.

   ```sh
   ./node_modules/.bin/chronozarr-viewer published --store ./store
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

## Use a store on another host

Give an absolute URL instead of a path:

```sh
./node_modules/.bin/chronozarr-viewer published --store https://example.org/my-store
```

The host of the store must send CORS headers. See [hosting](hosting.md).

## What the copy command does

- It refuses to overwrite a folder that exists.
- It does not check or copy data.
- It writes an empty catalog. A store that fails to load never falls back to the public sample data.

Without `--store`, the landing page shows "No store selected". To choose a store, open `demo/index.html?store=<URL>` in the copied folder. For an iframe, add `&embed=1`. See [embedding](embedding.md) for the controls and the origin restriction.

## Test the package

Run `npm run test:package` from `js/`. The test packs the package and installs it in a new temporary folder. It checks that the copy command refuses to overwrite. It then renders a synthetic store from the copied viewer under a URL subpath. It checks the time controls, the pixel inspector and embedded rendering. It also checks that the page makes no outside requests and logs no browser errors.
The test uses the development Playwright installation.
