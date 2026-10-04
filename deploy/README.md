# Publishing

Stores live in the R2 bucket `chronozarr-stores`, served at `https://data.chronozarr.org`; the demo at `https://chronozarr.org/demo/` shares the documentation Worker and serves copied JavaScript assets from the site build. wrangler is pinned in the root `package.json`: run `npm install` once at the repo root so `npx wrangler` resolves that copy, which went through the 7-day release-age quarantine (bare `npx wrangler` would otherwise fetch the latest release the moment it is published). All commands need `npx wrangler login` once. The header checklist, the upload order and recipes for other hosts (S3 with CloudFront, GCS, Source Cooperative) are in [docs/hosting.md](../docs/hosting.md).

## Stores

```bash
# one-time bucket setup (R2 must be enabled in the dashboard first)
npx wrangler r2 bucket create chronozarr-stores --location enam
npx wrangler r2 bucket cors set chronozarr-stores --file deploy/r2-cors.json --force
npx wrangler r2 bucket dev-url enable chronozarr-stores --force      # public r2.dev URL
npx wrangler r2 bucket domain add chronozarr-stores --domain data.chronozarr.org --zone-id <zone id>

# upload a store: the three-phase procedure in docs/hosting.md section 2 with the R2 put() of section 3.2
#   STORE=data/stores/ucayali_santa_maria/chronozarr-4  PREFIX=ucayali_santa_maria/chronozarr-4

# check the live URL: CORS, byte ranges, HEAD, caching, and a decode of every level
uv run chronozarr doctor https://data.chronozarr.org/ucayali_santa_maria/chronozarr-4
```

`deploy/r2-cors.json` uses wrangler's rule format (`rules[].allowed`, `exposeHeaders`), not the S3 CORS array.

Rules for a store:

- **Immutable.** A re-encode goes under a new prefix, never in place. Objects carry `Cache-Control: public, max-age=31536000, immutable`, so a rewritten object stays stale in browsers for up to a year.
- **Catalog last.** `js/demo/catalog.json` is the commit point. Upload the store, run `chronozarr doctor` on it, then `npm --prefix site run deploy` with the catalog change. `docs/hosting.md` section 2 has the strict metadata-last upload order. `scripts/upload_stores.sh` drives wrangler at about two objects per second, which suits a sharded store (93 objects) and not an unsharded one (about 5,900 objects, 17,000 with mask and coverage planes); for those use `uv run --with boto3 python scripts/r2_sync.py upload <aoi>/<store>` (R2's S3 API, 32 parallel puts, skips objects already present at the same size, chunks before metadata; needs an R2 API token with Object Read & Write in `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`). `r2_sync.py delete <aoi>/<store> --yes` removes a prefix.
- **Delete old prefixes afterwards** with `scripts/delete_stores.sh` (the bucket is on the 10 GB free tier). It takes its keys from `$ROOT/<aoi>/chronozarr` on disk, so point `ROOT` at a directory that still holds the old store under that name.

Rules on `data.chronozarr.org` (Cloudflare dashboard, Rules; both need zone write access, which the wrangler login token lacks):

- A Cache Rule with expression `(http.host eq "data.chronozarr.org")`: Eligible for cache, Edge TTL "Ignore cache-control header and use this TTL" 1 year, Browser TTL override 1 year. Verified 2026-09-30: `zarr.json` and a `206` shard range both go MISS then HIT.
- A Response Header Transform Rule with the same expression, Set static `Timing-Allow-Origin: *`.
- Put the expression in the expression editor or use the Hostname field. Pasted into a URI wildcard value it matches nothing, and the "may not apply to your traffic" warning for the R2 hostname is a false alarm.

The `chronozarr-4` upload check on 2026-10-01 reported 13 ok and 0 failures; this is a recorded check, not a fresh deployment verification. Settings and the other hosts are in `docs/hosting.md` section 3.

## Viewer

```bash
npm --prefix site run deploy
```

`js/_headers` sets `Cache-Control: no-cache` on viewer assets so a deploy applies on the next load.

The site build copies `js/demo/`, the reader, MapLibre layer, geolibre plugin, vendor modules and embed example into `site/docs/dist`. `/demo` redirects to `/demo/` with its query string preserved.
