# Publishing

Stores live in the R2 bucket `tileripper-stores`; the viewer is a Cloudflare Worker serving `js/` as static assets. All commands need `npx wrangler login` once.

```bash
# one-time bucket setup (R2 must be enabled in the dashboard first)
npx wrangler r2 bucket create tileripper-stores --location enam
npx wrangler r2 bucket cors set tileripper-stores --file deploy/r2-cors.json --force
npx wrangler r2 bucket dev-url enable tileripper-stores --force      # public r2.dev URL
npx wrangler r2 bucket domain add tileripper-stores --domain data.tileripper.com --zone-id <zone id>

# upload stores (4 parallel puts; re-runnable)
scripts/upload_stores.sh sahara_tamanrasset iowa_ames

# viewer
npx wrangler deploy
```

Stores are immutable: a re-encode goes under a new prefix, never in place.
