// Imported first by run.mjs, before Playwright is loaded: a run gets its own temporary directory, so that browser profiles
// and Playwright's artifacts cannot collide with (or be deleted by) another process that also runs Playwright on this
// machine (they share $TMPDIR otherwise). PLAYWRIGHT_ARTIFACTS_DIR is the variable the repository's Playwright Test runs
// use; the library itself places its artifacts under os.tmpdir(), which TMPDIR redirects.

import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const root = mkdtempSync(path.join(os.tmpdir(), 'chronozarr-bench-v03-'));
process.env.TMPDIR = root;
process.env.PLAYWRIGHT_ARTIFACTS_DIR = path.join(root, 'artifacts');
mkdirSync(process.env.PLAYWRIGHT_ARTIFACTS_DIR);
process.on('exit', () => rmSync(root, { recursive: true, force: true }));

export const PRIVATE_TMP = root;
