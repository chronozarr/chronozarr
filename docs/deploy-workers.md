# Deploy Note: Uvicorn Workers

## Systemd Unit Change

Edit the service override:

```bash
ssh jgearon@178.104.207.187 'sudo systemctl edit tileripper --force'
```

Add the `--workers 2` flag to the `ExecStart` line:

```ini
[Service]
ExecStart=
ExecStart=/home/jgearon/.local/bin/uvicorn spacetime.serve:app --host 127.0.0.1 --port 8765 --workers 2
```

Then restart the service:

```bash
sudo systemctl restart tileripper
```

## Per-Process State Implications

### 1. Band Cache Memory Doubles

The `_load_bands` function uses `@lru_cache` which is **per-process**. With 2 workers:

- Cache size doubles: `512 cached chunks × 2 workers × ~2MB/chunk = ~2GB` worst case
- CX22 VPS has 4GB RAM total

**Recommendation:** Reduce `_BAND_CACHE_SIZE` from 512 to 256 in `src/spacetime/api/v1.py`:

```python
_BAND_CACHE_SIZE = 256  # Was 512
```

### 2. Startup Events Run Per Worker

The `@app.on_event("startup")` handler runs in **each worker**, causing AOI discovery to happen twice.

- **Impact:** Minimal — it's a read-only scan of existing AOI directories
- **Risk:** None — no conflicts or data corruption possible

### 3. Rate Limiting & Usage Metering Are Inconsistent

Both rate limiting (`auth.py`) and usage metering (`metering.py`) use **in-memory per-process state**:

- **Rate limits:** A user could get 2× their quota by hitting different workers
- **Usage metering:** Per-key usage counts are split across workers, making the `/admin/usage` endpoint inaccurate

**Status:** Known limitation for this iteration.

**Future Fix:** Move to SQLite-backed metering using the same database as the job queue. This provides a shared state across all workers while avoiding the complexity of Redis or another external service.

---

*Deployed: 2024*
