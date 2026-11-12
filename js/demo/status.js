// What a render of the viewer left on the canvas, as one value. A pure function of what the render knows, with no DOM,
// so that the viewer's status (viewer.status, the data-frame attribute) has one definition that a test can read.

/**
 * The outcome of a render of the view:
 *  'none'         no store is open;
 *  'loading'      the frame asked for is not on the canvas (a coarser frame, another timestep or nothing stands in);
 *  'preview'      a fast scrub's coarser frame of the timestep asked for is on the canvas;
 *  'settled'      the frame asked for is on the canvas at the level the view settles on;
 *  'unavailable'  the frame asked for cannot be had: every cell it misses has failed for good.
 * The arguments are positional so that the viewer, which asks on every frame, allocates nothing for the call.
 * @param {boolean} open  a store is open
 * @param {boolean} showing  the complete frame of the timestep asked for is on the canvas, at the level drawn or finer
 * @param {boolean} sharp  that frame is at the level the view settles on, not a fast scrub's coarser level
 * @param {number} missing  how many cells of the frame asked for are not in memory
 * @param {boolean} failedForGood  every one of those cells has failed for good
 * @returns {'none' | 'loading' | 'preview' | 'settled' | 'unavailable'}
 */
export function frameOutcome(open, showing, sharp, missing, failedForGood) {
  if (!open) return 'none';
  if (showing) return sharp ? 'settled' : 'preview';
  return missing > 0 && failedForGood ? 'unavailable' : 'loading';
}
