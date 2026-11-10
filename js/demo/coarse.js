// Coarse-first frames, as pure planning with no DOM and no store.
//
// A frame that has to load is shown in stages: first a level coarse enough that its whole frame is small (a few
// hundred kB on the wire), which lands in a fraction of the time the target level needs, then each finer level down
// to the target. Every level has the same chunk size in pixels and half the resolution of the one before, so a cell
// (row, col) of one level lies inside cell (row >> 1, col >> 1) of the next coarser one.

import { DEFAULT_WIRE_RATIO } from './playback.js';

/** Largest decoded frame (valid pixels of the cells it needs) that still counts as small: about 0.7 MB compressed. */
export const COARSE_FRAME_BYTES = 1.5 * 1024 * 1024;

/**
 * The stages together may move at most this share of the target frame's bytes. Every level is a quarter of the next
 * one's size, so refining through all of them would add a third to the bytes (and the time to the complete frame);
 * the coarse levels are kept while they stay within the budget.
 */
export const COARSE_MAX_OVERHEAD = 0.15;

/**
 * A target frame that would arrive in less than this (at the measured or assumed bandwidth) is loaded directly. Each
 * stage costs round trips and the bytes of its level (staging every level down to the target adds about a third to the
 * bytes and a few hundred ms to the time to the complete frame), so it only pays for a frame that takes a while.
 */
export const COARSE_MIN_DIRECT_MS = 1000;

/** Bandwidth assumed before any transfer has been measured: 50 Mbit/s. */
export const ASSUMED_BANDWIDTH = (50 * 1e6) / 8;

/** The latency lead is this many store round trips, so the next stage's request latency overlaps the end of this stage's transfer. */
export const STAGE_LEAD_ROUND_TRIPS = 2;

/** The latency lead is at least this: a store that answers in a few ms (a local one) gets hardly any lead. */
export const STAGE_LEAD_MIN_MS = 30;

/** And at most this: a slow first response does not delay the stages for seconds. */
export const STAGE_LEAD_LATENCY_MAX_MS = 400;

/** The lead never exceeds this, even while the previous stage is still transferring. */
export const STAGE_LEAD_MAX_MS = 2000;

/** How long to give a stage to start responding, based on the time to open the store. */
export function stageLatencyLeadMs(roundTripMs) {
  return Math.min(STAGE_LEAD_LATENCY_MAX_MS, Math.max(STAGE_LEAD_MIN_MS, STAGE_LEAD_ROUND_TRIPS * roundTripMs));
}

/**
 * The longest a stage can hold the next one while its chunks are still arriving. Stores are unsharded by default,
 * so a chunk is one request that carries all its bytes. A stage that starts while the previous one's bytes are still arriving
 * shares the link with them, and the previous one lands later. The lead is the latency lead, STAGE_LEAD_ROUND_TRIPS times
 * `roundTripMs` (what it took to open the store, which is more than one round trip) within the limits above, when this stage's
 * transfer is shorter than that, and else the transfer time: `wireBytes` (what the stage still has to fetch) at `bandwidth`
 * (bytes per second), at most STAGE_LEAD_MAX_MS. The estimate of the bandwidth includes each request's wait for its first byte,
 * so the transfer time already holds the latency. A stage with no bytes to fetch, or no usable bandwidth, has the latency lead.
 */
export function stageLeadMs({ wireBytes, bandwidth, roundTripMs }) {
  const latency = stageLatencyLeadMs(roundTripMs);
  const transfer = wireBytes > 0 && bandwidth > 0 && Number.isFinite(bandwidth) ? (wireBytes * 1000) / bandwidth : 0;
  return Math.min(STAGE_LEAD_MAX_MS, Math.max(latency, transfer));
}

/**
 * The coarse loop (every timestep at the coarsest level, so that some whole frame is in memory wherever the user goes) plans as many
 * timesteps as the measured link carries in this many seconds: all of a short loop on any link a few MB/s or faster, a window around
 * the timestep on a slow one (about 20 steps of the 0.7 MB steps of the Ucayali store at 0.7 MB/s). It is planned again as the
 * playhead moves away from it (coarseLoopDrifted), so the loaded stretch follows the user.
 */
export const COARSE_LOOP_SECONDS = 20;

/** The coarse loop never plans fewer timesteps than this: two seconds of a movie at the default 4 steps/s, what playback buffers before it starts. */
export const COARSE_LOOP_MIN_STEPS = 8;

/**
 * How many timesteps of the coarse loop to plan, nearest first: what `bandwidth` (bytes per second) carries in `seconds` at
 * `bytesPerStep` on the wire, from COARSE_LOOP_MIN_STEPS to the whole loop of `count` timesteps. The whole loop while nothing has
 * been measured.
 */
export function coarseLoopSteps({ bytesPerStep, bandwidth, count, seconds = COARSE_LOOP_SECONDS }) {
  if (!(bandwidth > 0) || !(bytesPerStep > 0)) return count;
  return Math.min(count, Math.max(COARSE_LOOP_MIN_STEPS, Math.floor((seconds * bandwidth) / bytesPerStep)));
}

/**
 * Whether a coarse loop planned around timestep `planned` for `steps` timesteps should be planned again because the playhead (`t`)
 * is more than a quarter of that window away, either way around the loop. Never when it covers every timestep.
 */
export function coarseLoopDrifted({ t, planned, steps, count }) {
  if (steps >= count) return false;
  const gap = Math.abs(t - planned);
  return Math.min(gap, count - gap) > Math.floor(steps / 4);
}

/** The unique [row, col] cells of level `toLod` that cover the given cells of level `fromLod` (`toLod` >= `fromLod`). */
export function ancestorCells(cells, fromLod, toLod) {
  const shift = toLod - fromLod;
  const seen = new Set();
  const parents = [];
  for (const [row, col] of cells) {
    const parent = [row >> shift, col >> shift];
    const key = `${parent[0]}/${parent[1]}`;
    if (!seen.has(key)) {
      seen.add(key);
      parents.push(parent);
    }
  }
  return parents;
}

/**
 * The levels to load before the target of a frame, coarsest first (empty: load the target directly).
 *
 * The first is the finest level coarser than the target whose frame fits `maxBytes`, or the coarsest level when
 * none does; then come the levels between it and the target, as long as all the stages together stay within
 * `maxOverhead` of the target frame's bytes (so a frame too small to afford even the first gets none). Nothing is
 * staged either when the target frame would arrive within `minDirectMs` anyway (its decoded bytes times
 * `wireRatio`, over `bandwidth` in bytes per second).
 *
 * @param {{targetLod: number, coarsestLod: number, frameBytes: (lod: number) => number, bandwidth?: number, wireRatio?: number,
 *   maxBytes?: number, maxOverhead?: number, minDirectMs?: number}} plan
 *   `frameBytes(lod)`: decoded bytes of the cells of that level the frame needs
 * @returns {number[]}
 */
export function planCoarseStages({
  targetLod,
  coarsestLod,
  frameBytes,
  bandwidth = ASSUMED_BANDWIDTH,
  wireRatio = DEFAULT_WIRE_RATIO,
  maxBytes = COARSE_FRAME_BYTES,
  maxOverhead = COARSE_MAX_OVERHEAD,
  minDirectMs = COARSE_MIN_DIRECT_MS,
}) {
  if (targetLod >= coarsestLod) return [];
  const target = frameBytes(targetLod);
  if ((target * wireRatio * 1000) / bandwidth < minDirectMs) return [];
  let first = coarsestLod;
  for (let lod = targetLod + 1; lod < coarsestLod; lod++) {
    if (frameBytes(lod) <= maxBytes) {
      first = lod;
      break;
    }
  }
  const stages = [];
  let spent = 0;
  for (let lod = first; lod > targetLod; lod--) {
    spent += frameBytes(lod);
    if (spent > maxOverhead * target) break;
    stages.push(lod);
  }
  return stages;
}
