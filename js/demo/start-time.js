// Where the full viewer starts in time when nobody asked for a timestep.

/**
 * Address parameters that name a store or describe a view. An address with any of them is a link to a view, and a t
 * that it leaves out means the first timestep, as it always has. Only an address with none of them is a bare visit.
 */
const VIEW_PARAMS = ['store', 't', 'p', 'b', 'r', 'z', 'c'];

/** Whether the page was opened with no store and no view in its address, so the viewer may choose the start. */
export function isBareVisit(search) {
  const params = new URLSearchParams(search);
  return !VIEW_PARAMS.some((name) => params.has(name));
}

/** The share of observed (or, without a coverage plane, valid) pixels that makes a timestep worth starting on. */
export const COVERED_SHARE = 0.5;
/** How many of the newest timesteps the rule looks at. */
export const LOOKBACK = 24;

/**
 * The latest timestep with meaningful coverage. Among the newest `lookback` timesteps, it is the latest in which at
 * least `share` of the pixels of the coarsest level are observed (coverage above 0) or, in a store without a coverage
 * plane, valid (mask 1). If none reaches `share`, it is the one with the largest share. A store with neither plane
 * records nothing about observations, so it starts at its last timestep and nothing is read.
 *
 * Reads the coverage or mask chunks of the coarsest level, newest timestep first, and stops at the first that
 * qualifies: one small request for a store whose last timestep is well covered. Padding outside the level is not
 * counted. `store` is an open reader store (`times`, `levels`, `hasCoverage`, `hasMask`, `getCoverage`, `getMask`).
 */
export async function latestCoveredTime(store, { share = COVERED_SHARE, lookback = LOOKBACK, signal } = {}) {
  const last = store.times.length - 1;
  const kind = store.hasCoverage ? 'coverage' : store.hasMask ? 'mask' : null;
  if (kind === null) return last;
  const level = store.levels[store.levels.length - 1];
  const read = (row, col, t) => (kind === 'coverage' ? store.getCoverage(level.lod, row, col, t, { signal }) : store.getMask(level.lod, row, col, t, { signal }));
  let best = { t: last, share: -1 };
  for (let t = last; t >= Math.max(0, last - lookback + 1); t--) {
    let covered = 0;
    let total = 0;
    for (let row = 0; row < level.gridRows; row++) {
      for (let col = 0; col < level.gridCols; col++) {
        const chunk = await read(row, col, t);
        const height = Math.min(level.chunkHeight, level.height - row * level.chunkHeight);
        const width = Math.min(level.chunkWidth, level.width - col * level.chunkWidth);
        total += height * width;
        if (!chunk) continue; // an absent chunk is all fill: nothing observed
        for (let y = 0; y < height; y++) {
          for (let x = 0; x < width; x++) if (chunk[y * level.chunkWidth + x] > 0) covered++;
        }
      }
    }
    const fraction = total > 0 ? covered / total : 0;
    if (fraction >= share) return t;
    if (fraction > best.share) best = { t, share: fraction };
  }
  return best.t;
}
