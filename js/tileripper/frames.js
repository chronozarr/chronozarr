// Which frame the viewer shows, as pure functions. A frame is one level and one timestep for every cell the
// view needs; it is shown whole or not at all. Resolution may drop, completeness never does.

/**
 * The frame to show for timestep `t` when the viewer wants level `targetLod`:
 *  1. the target level itself, when every cell of it is in memory ("target");
 *  2. else the finest coarser level for which that is true ("fallback"): the same timestep, less resolution;
 *  3. else the frame that is already on screen, if it is still complete for the current view ("previous");
 *  4. else nothing: the canvas is left alone.
 * `isReady(lod, t)` says whether the whole frame of that level and timestep is in memory.
 *
 * @param {{targetLod: number, coarsestLod: number, t: number, isReady: (lod: number, t: number) => boolean,
 *   previous?: {lod: number, t: number} | null}} options
 * @returns {{lod: number, t: number, kind: 'target' | 'fallback' | 'previous'} | null}
 */
export function chooseFrame({ targetLod, coarsestLod, t, isReady, previous = null }) {
  for (let lod = targetLod; lod <= coarsestLod; lod++) {
    if (isReady(lod, t)) return { lod, t, kind: lod === targetLod ? 'target' : 'fallback' };
  }
  const sameAsTried = previous && previous.t === t && previous.lod >= targetLod;
  if (previous && !sameAsTried && isReady(previous.lod, previous.t)) return { lod: previous.lod, t: previous.t, kind: 'previous' };
  return null;
}
