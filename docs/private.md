# Viewing a private store

A viewer link on `chronozarr.org` shows a store only if the browser can read the store's files. The viewer has no login and no server. This page explains what that means for data that must not be public. It lists the access paths that work with static hosting.

Statements about hosts describe general provider behaviour. Check your provider's documentation. This repository tested none of the auth products named here. Statements about chronozarr come from the code and say where.

## 1. What a viewer link exposes

`https://chronozarr.org/demo/?store=<url>` loads the viewer page from chronozarr.org. The page then reads the store directly from `<url>` in the viewer's browser. chronozarr.org does not host, copy or proxy the data.

Three facts follow:

- The store URL is the access control. Anyone who can fetch `<url>` can read every object, with or without the viewer.
- The viewer is not a gate. A public store stays public if you never share the viewer link.
- Credentials in the Python kernel do not reach the browser. A notebook that reads a bucket with your AWS keys does not let the viewer read the bucket.

The browser request for the viewer page includes the query string, so `?store=` reaches the host of chronozarr.org. `site/worker.js` does not read or log it. This repository cannot show what Cloudflare logs at the edge.

A private bucket behind a public CDN URL is a public store. The [S3 and CloudFront recipe](hosting-providers.md#amazon-s3-with-cloudfront) blocks public access to the bucket and leaves the distribution open. Anyone with the distribution URL can read the store.

## 2. Why one presigned URL is not enough

A store is many objects: group and array metadata, plus one object per chunk or shard. The 117-month imagery store has about 5,900 objects when unsharded.

An S3 presigned URL signs one object key. A GCS signed URL signs one object. A URL for `zarr.json` returns 403 for every chunk.

The viewer cannot take a list of signed URLs. It builds each object URL from the store base URL and the Zarr key (`#url` in `js/chronozarr/http.js`). It copies the query string of the base URL onto every object URL.

A token works for a store only if one token is valid for every object under the store prefix.

## 3. Paths that work

| Path | Who can view | Works with the hosted viewer | Status |
|---|---|---|---|
| 3.1 Local copy served on 127.0.0.1 | You, on this machine | Yes | Works today |
| 3.2 Viewer and store on one origin behind CDN auth | Holders of the CDN credential | No, self-hosted viewer | Works by design, not tested against a host |
| 3.3 Prefix-wide token in `store=` | Holders of the link | Yes | Works, with leaks (section 4) |
| 3.4 Cookies across origins | Holders of the cookie | No | Not supported |
| Authenticated bucket proxy on 127.0.0.1 | You | Yes | Not implemented |

### 3.1 Local copy served on 127.0.0.1

The data never leaves your machine after the copy. Your cloud credentials stay in the tool that copies the files.

1. Copy the store to local disk with your own credentials.

   ```sh
   aws s3 sync s3://my-bucket/my_aoi/chronozarr-2 ./chronozarr-2 --profile my-profile
   ```

2. Check the copy.

   ```sh
   chronozarr validate ./chronozarr-2
   ```

3. Serve the copy and print the viewer link.

   ```python
   from chronozarr.view import serve_store, viewer_url

   server = serve_store("./chronozarr-2")
   print(viewer_url(server.url))  # https://chronozarr.org/demo/?store=http%3A%2F%2F127.0.0.1%3A<port>%2Fchronozarr-2
   ```

   In a local notebook, `chronozarr.view("./chronozarr-2")` shows the same viewer in an iframe.

4. Call `server.close()` when you are done. The server also stops when the Python process ends.

The limits:

- `serve_store` and `view` take a local directory or an `http(s)` URL. They reject `s3://` and other URIs. They do not read a bucket on demand.
- The copy costs disk space and transfer charges from the bucket's host.
- Only a browser on the same machine reaches `127.0.0.1` directly. Remote kernels need a browser-reachable route. With `jupyter-server-proxy` and `viewer_dir`, JupyterHub uses a same-origin proxy; other environments can use port forwarding or `base_url`. See [remote notebooks](notebooks.md#remote-notebooks).
- The loopback server answers with `Access-Control-Allow-Origin: *` (`_CORS_HEADERS` in `src/chronozarr/view.py`). While it runs, a web page open in the same browser can read the store if it knows the port. Chrome asks permission before a public page reaches a local address. Close the server when you finish.
- The viewer page still loads from chronozarr.org. To avoid that, serve the viewer yourself ([viewer-distribution.md](viewer-distribution.md)).

### 3.2 Viewer and store on one origin behind CDN auth

A browser sends cookies and HTTP authentication with a request to the origin of the page. A request to another origin carries none, unless the page asks for it.

The viewer reads the store with `fetch(new Request(url, ...))` and sets no `credentials` option (`#send` in `js/chronozarr/http.js`). The default is `same-origin`. A store on the same origin as the viewer therefore receives your session cookie. The request needs no CORS headers.

1. Copy the viewer and the store into one folder ([viewer-distribution.md](viewer-distribution.md)).
2. Upload the folder to one hostname, for example `https://data.example.org/`.
3. Put the host's auth in front of the whole hostname. Examples: CloudFront signed cookies, Cloudflare Access, HTTP basic auth.
4. Open `https://data.example.org/published/demo/index.html?store=/published/store`.

The auth now covers the viewer files and the store. The `store=` value holds a path and no secret.

The limits:

- The auth must also allow `Range` requests, if the store is sharded, and must not add `Content-Encoding` ([hosting-requirements.md](hosting-requirements.md#checklist)).
- `chronozarr doctor` cannot send a cookie. Run it on a public test copy before you add auth, or use the network tab of the browser.
- An expired session may show as a failed request, not as a login page. The viewer then shows "Could not open store". This repository did not test any host's expiry behaviour.
- Each provider names its own cookie domain and path rules. Follow the provider's documentation.

### 3.3 Prefix-wide token in `store=`

If the host accepts one query token for the whole prefix, put the signed URL in `store=`. [embedding.md](embedding.md#6-stores-and-access) lists the token types.

This path works with the hosted viewer and any page. It has the widest leaks, because the token is part of the link. Section 4 lists where the token appears.

Use a short lifetime and a read-only token scoped to the store prefix. When the token expires, the host refuses new requests and the link stops working. The viewer does not check the token or its expiry.

### 3.4 Cookies across origins

Not supported with the hosted viewer. The viewer on `chronozarr.org` sends no credentials to a store on another origin. No URL parameter turns them on.

A cross-origin request with cookies needs all of these:

- the page asks for it: `credentials: 'include'`
- the response has `Access-Control-Allow-Origin` set to the exact viewer origin, not `*`
- the response has `Access-Control-Allow-Credentials: true`
- the browser accepts the cookie as a third-party cookie, which many browsers block by default

The reader accepts a `fetch` option for pages that you write. It works with `openStore(url, { fetch })` and with `storeOptions: { fetch }` of the MapLibre layer. Neither the packaged viewer nor the hosted viewer sets it.

```js
const fetchWithCookies = (request) => fetch(new Request(request, { credentials: 'include' }));
const store = await openStore(url, { fetch: fetchWithCookies });
```

This code was not run against a real cross-origin host. A custom header such as `Authorization` also works through `fetch`. The browser then sends a preflight request before every read, and the host must allow that header.

The CORS settings in the [hosting recipes](hosting-providers.md) use `*` and no credentials. They are for public stores. Do not copy them to a private store.

### Not implemented: authenticated bucket proxy on 127.0.0.1

`chronozarr preview` and `chronozarr.view` serve a local store copy. No command reads a private bucket with your credentials and proxies it through 127.0.0.1. That would need a loopback server that signs each upstream request. Use section 3.1 until such a proxy exists.

## 4. Keeping credentials out of links, notebooks and logs

The viewer never needs an AWS key, a password or a bucket policy. A browser cannot use them. Do not paste a key into a URL, a notebook or a chat.

A URL with a user name and password, such as `https://user:pass@host/store`, fails. The browser refuses to build a request from it.

Where a token in `store=` ends up, and how to limit it:

| Place | What happens today | What to do |
|---|---|---|
| Address bar and history | The original `?store=<signed-url>` navigation contains the token. After the store opens, the viewer replaces the displayed URL with the store origin and path, without its query. A copied original link still contains the token. | Treat every copied signed link as the token. Use a short lifetime. |
| Request for the viewer page | The query string goes to the host of the page | Use 3.1 or 3.2 when the host of the viewer must not see the token |
| Browser console | Reader retry messages, failed-open messages and displayed URLs redact the query. Some diagnostic paths still log the `FetchError` object, whose `url` property is the request URL. | Do not share developer-tools output or screenshots when a signed store has failed. |
| Error overlay | Viewer error text redacts HTTP URL queries before it is shown. | The path and host can still identify the store; do not use the overlay as a secret-safe sharing channel. |
| Store menu | A catalog-page label for an opened store omits the query. Its internal option value remains the URL used to read the store. | Do not treat the page's JavaScript state as a credential vault. |
| Embedded page | The iframe URL contains the `store` value, so the token is saved with the embedding page or notebook output. The wordmark copies the store address without its query; a signed store needs fresh authorization there. | Use a token that is safe for every viewer of the embedded page, or use a same-origin protected viewer. |
| Notebook output | `view(url)` writes the URL into the iframe `src` and the caption of the cell output (`view` in `src/chronozarr/view.py`), and Jupyter saves it in the `.ipynb` file | Use `view` with a local directory (3.1). Clear outputs before you commit or share a notebook |
| Python and CLI tools | `HttpStore` joins each Zarr key onto the URL path and carries the query onto every object request. `open_store`, `info` and `doctor` work with prefix-wide token URLs; their displayed URLs redact the query. | Keep the original command argument out of shell history and CI logs. |
| Shell history and CI logs | A URL on the command line is recorded | Use a profile or an environment variable for credentials, not a URL |

Check a link before you share it:

1. Open it in a private browser window with no cookies.
2. Confirm that the viewer opens the store only when the link is valid.
3. Wait for the token to expire and confirm that the store stops loading.

Expiry and access checks happen at the host. The viewer does none.
