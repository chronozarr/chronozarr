// Constants of the v0.3 delivery benchmark: what is measured, on which data, at which views and links. Nothing here
// starts a process; run.mjs, session.mjs and values.mjs read it.

import path from 'node:path';
import { PROFILES, REPO_ROOT } from '../lib/harness.mjs';

export { PROFILES, REPO_ROOT };

/** Where the store, the COGs and the results live. The default is the repository's own gitignored `data/`. */
export const DATA_ROOT = path.resolve(process.env.BENCH_DATA ?? path.join(REPO_ROOT, 'data'));
export const STORE_REL = 'stores/ucayali_santa_maria_v03';
export const COG_REL = 'bench/v03/cogs';
export const STORE_DIR = path.join(DATA_ROOT, STORE_REL);
export const COG_DIR = path.join(DATA_ROOT, COG_REL);
export const RESULTS_ROOT = path.join(DATA_ROOT, 'bench/v03/results');

/** The browser: one window size and pixel ratio for every system. */
export const VIEWPORT = { width: 1440, height: 900, deviceScaleFactor: 2 };

/** Link profiles of the task: unthrottled, 50 Mbit/s + 40 ms, 10 Mbit/s + 100 ms (lib/harness.mjs applies them through CDP). */
export const PROFILE_NAMES = ['natural', '50Mbit-40ms', '10Mbit-100ms'];

/**
 * The study area is the Ucayali store's footprint, EPSG:32718, level 0 = 2759 x 2765 px at 10 m, cells of 512 px.
 * `cssPxPerL0` is the on-screen size of a level-0 pixel in CSS pixels; both views are centred and drawn at this ground scale.
 *   overview  the whole AOI fits the window height (880 of 900 CSS px)
 *   zoom      a window of 993 x 621 level-0 pixels centred on the corner shared by cells (2,2), (2,3), (3,2) and (3,3):
 *             four 512 px cells of level 0 and nothing else
 * Every system chooses its own level of detail for a view, and they differ (20 m, 40 m and 19 m pixels in the overview,
 * run.mjs records them). The two "matched" views repeat a view with the systems that can be held to a pixel size held to it:
 *   overview-matched  A on level 2 (40 m, what zarr-layer chooses by itself) and C on tiles of at most zoom 11 (38 m)
 *   zoom-matched      C on tiles of at most zoom 13 (9.5 m, the store's 10 m); A and B already draw level 0, so only C runs
 * `pin` is what a driver passes to its system: A's `lod`, C's raster source `maxzoom`. zarr-layer has no such option.
 */
export const AOI = { bounds: [485650, 9142230, 513240, 9169880], crs: 'EPSG:32718', width: 2759, height: 2765, resolution: 10 };
export const VIEWS = {
  overview: { name: 'overview', centerPx: [AOI.width / 2, AOI.height / 2], cssPxPerL0: 880 / AOI.height },
  zoom: { name: 'zoom', centerPx: [1536, 1536], cssPxPerL0: 1.45 },
  'overview-matched': { name: 'overview-matched', centerPx: [AOI.width / 2, AOI.height / 2], cssPxPerL0: 880 / AOI.height, pin: { A: { lod: 2 }, C: { maxzoom: 11 } } },
  'zoom-matched': { name: 'zoom-matched', centerPx: [1536, 1536], cssPxPerL0: 1.45, only: ['C'], pin: { C: { maxzoom: 13 } } },
};

/** Timesteps (monthly index into the store's 117 dates): open at START_T, step forward STEPS dates, step back, then jump JUMP_DISTANCE dates away. */
export const START_T = 10;
export const STEPS = 30;
export const JUMP_DISTANCE = 50;

/**
 * Pause between a complete frame and the next input. It is longer than the viewer's scrub window (300 ms,
 * js/demo/scrub.js), so each step is a separate tap and never a scrub that would lower the viewer's level.
 */
export const THINK_MS = 400;
export const TRAILING_IDLE_MS = 3000;
export const OPEN_TIMEOUT_MS = 300000;
export const STEP_TIMEOUT_MS = 180000;

/** A run is discarded when the median animation-frame gap of its idle windows is outside this range (nominal 16.7 ms). */
export const RAF_NOMINAL_MS = { min: 15.5, max: 18 };

/**
 * TiTiler from its official image, pinned by digest (the multi-arch index of tag 2.4.0, created 2026-09-21). One
 * container per session, started cold and then warmed (servers.mjs). The image starts gunicorn with WEB_CONCURRENCY
 * workers (the image's own default is 1); `workers` below is what the benchmark uses unless --titiler-workers says otherwise.
 * The GDAL settings are the "Recommended Configuration for dynamic tiling" of TiTiler's performance-tuning page
 * (developmentseed.org/titiler/advanced/performance_tuning/, read 2026-10-09), except that GDAL_INGESTED_BYTES_AT_OPEN is
 * set to 32 KB, which covers the header of these COGs (all IFDs end before byte 2,870 of a 57 MB file), as that page says to tune it to the header size.
 */
export const TITILER = {
  image: 'ghcr.io/developmentseed/titiler',
  tag: '2.4.0',
  digest: 'sha256:5b739117ff60d3cef9b0e0b8d9f4d628032c00a4965117e3f745cadc79fa7287',
  workers: 4,
  env: {
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS: '.tif',
    GDAL_CACHEMAX: '200',
    CPL_VSIL_CURL_CACHE_SIZE: '200000000',
    GDAL_BAND_BLOCK_CACHE: 'HASHSET',
    GDAL_DISABLE_READDIR_ON_OPEN: 'EMPTY_DIR',
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES: 'YES',
    GDAL_HTTP_MULTIPLEX: 'YES',
    GDAL_HTTP_VERSION: '2',
    GDAL_INGESTED_BYTES_AT_OPEN: '32768',
    VSI_CACHE: 'TRUE',
    VSI_CACHE_SIZE: '5000000',
  },
  /** COGs the container reads before a session (dates 100 to 116; steps use 10 to 60), so that its Python and GDAL start-up is not charged to the first tile. */
  warmupDates: Array.from({ length: 17 }, (_, i) => 100 + i),
};

/** Tile request of system C: bands 3, 2, 1 of the COG (B04, B03, B02) stretched linearly from 0..3000 to 8 bit, 512 px tiles for 256 px tile cells (retina). */
export const TITILER_TILE_QUERY = 'bidx=3&bidx=2&bidx=1&rescale=0,3000&tilesize=512&resampling=nearest';

export const SYSTEMS = {
  A: { id: 'A', name: 'chronozarr viewer', page: '/js/demo/index.html', driver: 'driver-a.js' },
  B: { id: 'B', name: 'zarr-layer', page: '/bench/zarr-layer/page.html', driver: 'driver-b.js' },
  C: { id: 'C', name: 'COG per date + TiTiler', page: '/bench/v03/page-c.html', driver: 'driver-c.js' },
};
