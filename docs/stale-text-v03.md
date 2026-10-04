# v0.3 stale-text audit

2026-10-03. Current writer/read-path descriptions and executable v0.3 fixture configuration were corrected. The excluded profile investigation and archived documents retain their historical content. Every remaining search hit is accounted for below. Line numbers refer to this release preparation checkout. The ignored local napkin is explicitly historical below its new release entry.

| File | Lines | Why retained |
|---|---|---|
| `bench/adoption/README.md` | 37 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `bench/adoption/results.json` | 238, 239, 3787, 3788, 4841, 4842, 6529, 6530, 10078, 10079 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `bench/rendered/README.md` | 61 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `bench/rendered/results.json` | 711, 712, 14348, 14349, 17652, 17653, 24468, 24469, 38105, 38106, 41409, 41410 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `docs/comparisons.md` | 51 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `docs/positioning.md` | 5, 34, 48, 53, 55, 66, 68, 73, 74, 83, 90, 101 | Pre-existing untracked v0.2 positioning investigation; preserved outside release commits. |
| `examples/bring_your_data/extended/results/browser-frames.json` | 11, 12, 30, 31, 49, 50, 68, 69, 87, 88, 106, 107, 125, 126, 144, 145, 163, 164, 182, 183, 201, 202, 220, 221 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `examples/bring_your_data/large/results/browser.json` | 8, 13, 14, 30, 35, 36, 52, 57, 58, 74, 79, 80, 96, 101, 102, 118, 123, 124, 140, 145, 146, 162, 167, 168, 184, 189, 190, 206, 211, 212, 228, 233, 234, 250, 255, 256, 281, 282, 314, 315, 333, 334, 352, 353, 371, 372, 390, 391, 409, 410, 428, 429, 447, 448, 466, 467, 485, 486, 504, 505, 523, 524 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `examples/bring_your_data/large/results/verification.json` | 22 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `examples/bring_your_data/results/clean-checkout-20261002/pixel-history.txt` | 16 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `examples/bring_your_data/results/lake-mead-2020/README.md` | 38 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `js/demo/viewer.js` | 1686, 1700 | SVG alignment or URL fragments, unrelated to temporal storage. |
| `js/test/decoder-cache.test.js` | 390 | Migration/rejection fixtures or assertions that reconstruction is absent; the cache-test URL is an arbitrary identifier. |
| `js/test/decoder-v03.test.js` | 88, 110 | Migration/rejection fixtures or assertions that reconstruction is absent; the cache-test URL is an arbitrary identifier. |
| `js/test/maplibre-shader.test.js` | 15 | Migration/rejection fixtures or assertions that reconstruction is absent; the cache-test URL is an arbitrary identifier. |
| `js/test/viewer-renderer.test.js` | 21 | Migration/rejection fixtures or assertions that reconstruction is absent; the cache-test URL is an arbitrary identifier. |
| `docs/archive/bench_append_v02.py` (original script lines) | 6, 75, 227, 229, 231, 238, 239, 320, 322, 323, 342, 399 | Historical v0.2 experiment; header requires a pinned v0.2 checkout, not the v0.3 API. |
| `site/README.md` | 51 | Explicitly historical observations or recorded v0.2 result keys, preserved as evidence. |
| `site/scripts/sync-content.mjs` | 28, 31 | SVG alignment or URL fragments, unrelated to temporal storage. |
| `spec/CHRONOZARR.md` | 366, 396, 398, 399, 401, 402 | Changes table or explicit unsupported-version error example; describes removal or rejection. |
| `src/chronozarr/_convert_legacy.py` | 66, 68, 69, 70, 74, 76, 77, 78, 79, 131 | Explicit legacy conversion importer, invoked only during migration. |
| `tests/synthetic.py` | 89, 90, 91, 93 | Nominal comparison schedule for optional volatility; no temporal storage reconstruction. |
| `tests/test_append.py` | 27, 268 | Nominal comparison schedule for optional volatility; no temporal storage reconstruction. |
| `tests/test_doctor.py` | 162 | Migration/rejection fixtures or assertions that reconstruction is absent; the cache-test URL is an arbitrary identifier. |
| `tests/test_format_encoding.py` | 93, 100, 119, 129, 131, 132, 133, 143 | Migration/rejection fixtures or assertions that reconstruction is absent; the cache-test URL is an arbitrary identifier. |
| `tests/test_stac.py` | 100 | Migration/rejection fixtures or assertions that reconstruction is absent; the cache-test URL is an arbitrary identifier. |

## Python API follow-up for PR 17

The initial word sweep missed removed keyword arguments and attributes. The follow-up searches `src`, `scripts`, `examples`, `tests` and `js` for `encoding=`, `.temporal`, `anchor_interval`, `report.encoding`, `temporal=`, `report.selection`, `compute_anchor_schedule`, `is_anchor`, `isAnchor`, `anchor_indices` and `delta_reference`.

Current example and notebook calls use only v0.3 parameters. Remaining hits have these meanings:

| Files | Reason |
|---|---|
| `src/chronozarr/_convert_legacy.py` | Reads v0.2 metadata only during explicit conversion. |
| `src/chronozarr/backend.py`, `tests/test_convert.py` | xarray variable/Zarr encoding, unrelated to removed chronozarr arguments. |
| `src/chronozarr/stac.py`, `_convert_manifest.py` | UTF-8 file/text encoding. |
| `tests/test_format_encoding.py`, `test_schema.py`, `test_stac.py`, `js/test/decoder-v03.test.js` | Legacy migration fixtures and assertions that v0.3 rejects or omits old metadata. |
| `examples/bring_your_data/large/results/verification.json` | Recorded historical v0.2 result; preserved unchanged. |

The previous append benchmark's temporal APIs are preserved only in `docs/archive/bench_append_v02.py`; the executable script now benchmarks true-value appends. The shard-index location is normalized once because zarr 3.1.6 supplies an enum and zarr 3.4.0 supplies a string. A new installed-zarr sharded-read regression exercises the actual codec and index cache without mocking the type.

Follow-up validation: 548 unit tests passed on Python 3.11/zarr 3.1.6 and Python 3.13/zarr 3.4.0. The installed-wheel notebook preparation passed on Python 3.11 with leafmap 0.63.1 and geemap 0.37.2. No lockfile or Python-requirement change was made.
