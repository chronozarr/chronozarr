// How fast the user is scrubbing the timeline, and the level the timesteps already loaded ahead of it can carry, with no DOM and injectable time.
//
// A scrub is a run of manual steps (a drag on the timeline, held or repeated arrow keys). Its speed is the distance
// covered per second over the last SCRUB_WINDOW_MS. 300 ms holds three or more steps at 7 steps/s and still follows
// a change of pace within a third of a second. The speed is measured from one step to the latest instead of counting
// steps per window, so a steady drag reads the same whichever way its steps fall against the window edges.
// With no step for SCRUB_WINDOW_MS the scrub is over and the speed is 0.
//
// The level follows the buffer, not a throughput estimate (Huang et al., SIGCOMM 2014): a level is drawn while the
// timesteps ahead of the one on screen are already in memory at it, as many as the scrub will use in the next
// LOOKAHEAD_SECONDS. An estimate sees only what the viewer itself asks for, so a viewer that has dropped to a coarser
// level asks for less, reads a slower link and drops further (Huang et al., IMC 2012); what is in memory ahead does
// not depend on what was requested.

/** The steps this close to the latest one make up the speed; with no step for this long the user has stopped scrubbing. */
export const SCRUB_WINDOW_MS = 300;

/** The elapsed time the distance is divided by is never less than this, so two events a frame apart do not read as a sprint. */
export const MIN_SPAN_MS = SCRUB_WINDOW_MS / 2;

export class ScrubSpeed {
  #steps = [];

  /** The time of the latest step in ms (the clock of `note`), or null before the first. */
  get latestAt() {
    return this.#steps.at(-1)?.at ?? null;
  }

  /** A step of `distance` (>= 1) timesteps at time `at` in ms. */
  note(at, distance) {
    this.#steps.push({ at, distance });
    const from = at - SCRUB_WINDOW_MS;
    const first = this.#steps.findIndex((step) => step.at >= from);
    this.#steps.splice(0, first);
  }

  /**
   * Timesteps per second at time `now`: the distance covered after the first step in the window, over the time from
   * that step to the latest (at least MIN_SPAN_MS). It stays as it is between steps and is 0 for a single step
   * (a tap is not a scrub) and once SCRUB_WINDOW_MS has passed without one.
   */
  speed(now) {
    const last = this.#steps.at(-1);
    if (!last || now - last.at >= SCRUB_WINDOW_MS) return 0;
    const first = this.#steps[0];
    let distance = 0;
    for (const step of this.#steps.slice(1)) distance += step.distance;
    return (distance * 1000) / Math.max(last.at - first.at, MIN_SPAN_MS);
  }

  /** The scrub is over (the pointer was released): the next step starts a new one. */
  reset() {
    this.#steps = [];
  }
}

/**
 * A level carries the scrub while the timesteps ahead that are in memory at it last this long at the scrub's speed:
 * the lookahead, long enough for the batch of timesteps in flight (BUFFER_BATCH, viewer.js) and one slow object to
 * land. It was chosen by measurement on the overview of the live store at an emulated 40 MB/s, a drag of 150 ms/step,
 * two rounds each: 0.75, 1 and 1.5 s drew the same frames (level 1 on 8 to 13 of 115, level 2 on 101 to 108), and
 * 1 s fetched the fewest level-1 chunks, 177 to 186 against 322 to 466 (the viewer without the probe fetched 133 to 146
 * with its idle prefetch). Level 1 cannot be carried at that speed on that link, so those chunks are the probe's cost.
 */
export const LOOKAHEAD_SECONDS = 1;

/**
 * The level in use is kept until what is ahead of it falls below this fraction of the lookahead (a third of a
 * second of scrub), and a sharper level is taken only when it holds all of the lookahead: between the two marks
 * nothing changes. A half instead of a third drew the same frames at 40 MB/s; on the real link it dropped to level 3
 * for 12 frames before level 2 had loaded where a third did for 2 (one round each).
 */
export const LOW_MARK = 1 / 3;

/** The level in use is loaded this many lookaheads ahead, so that it keeps covering one while the sharper level is probed. */
export const FILL_LOOKAHEADS = 2;

/** The timesteps after `t` that a scrub going `direction` (1 or -1) reaches, nearest first, at most `limit` of them, stopping at the ends of the axis. */
export function stepsAhead({ t, direction, count, limit }) {
  const steps = [];
  for (let next = t + direction; steps.length < limit && next >= 0 && next < count; next += direction) steps.push(next);
  return steps;
}

/** How many of `steps` (nearest first) are ready in a row from the nearest: a step that is not ends the run, whatever lies beyond it. */
export function readyRun(steps, isReady) {
  let run = 0;
  while (run < steps.length && isReady(steps[run])) run++;
  return run;
}

/**
 * How many timesteps ahead a level has to hold for a scrub at `speed` timesteps per second: what the scrub uses in
 * LOOKAHEAD_SECONDS, but not more than are left in its direction (a scrub at the end of the axis has nothing more to wait for).
 * 0 when nobody scrubs.
 */
export function scrubNeed({ speed, t, direction, count }) {
  const left = direction > 0 ? count - 1 - t : t;
  return Math.min(Math.ceil(speed * LOOKAHEAD_SECONDS), left);
}

/**
 * The levels a scrub can draw at, sharpest first: the normal level `baseLod`, then each coarser one down to `deepestLod`
 * (the level of a view four times further out, never coarser) that has fewer cells than the one before, so fewer chunks
 * per timestep. A level with as many cells only costs resolution: zoomed in, a view's level 1 has the same four cells as its level 0.
 * @param {{baseLod: number, deepestLod: number, cellCount: (lod: number) => number}} options
 */
export function scrubLevels({ baseLod, deepestLod, cellCount }) {
  const levels = [baseLod];
  for (let lod = baseLod + 1; lod <= deepestLod; lod++) if (cellCount(lod) < cellCount(levels.at(-1))) levels.push(lod);
  return levels;
}

/**
 * The level to draw and load at for the next step of a scrub that needs `need` timesteps ahead (scrubNeed), given
 * `held`, the level of the step before (the normal level when the scrub has just begun).
 * - A sharper level than the held one is taken when it holds all `need` (the sharpest that does).
 * - Else the held level is kept while it holds at least LOW_MARK of `need`; between the two marks nothing changes, so the
 *   level does not flip with every step that lands or is used up.
 * - Else the sharpest coarser level that holds all `need`, or the deepest level when none does: it is the cheapest to load.
 * With nothing to wait for (`need` 0: no scrub, or the end of the axis) every level holds enough and the normal level is chosen.
 * @param {{levels: number[], held?: number | null, need: number, ready: (lod: number) => number}} options
 *   `levels` from scrubLevels; `ready(lod)` is how many timesteps in a row ahead are in memory at `lod` (at least `need` is counted).
 */
export function chooseScrubLevel({ levels, held = null, need, ready }) {
  const at = Math.max(0, levels.indexOf(held));
  for (let i = 0; i < at; i++) if (ready(levels[i]) >= need) return levels[i];
  if (ready(levels[at]) >= Math.ceil(LOW_MARK * need)) return levels[at];
  for (let i = at + 1; i < levels.length - 1; i++) if (ready(levels[i]) >= need) return levels[i];
  return levels.at(-1);
}

/**
 * A scrub that needs more timesteps ahead than this is faster than the viewer can load ahead of it at any level but
 * the coarse loop (the reader's idle prefetch horizon is 12 timesteps either side of t, decoder.js). It does not probe:
 * a sharper level that cannot get ahead of the scrub is bytes for frames nobody sees.
 */
export const PROBE_MAX_NEED = 12;

/**
 * What to load for a scrub that is drawing at `lod`: the timestep on screen and as many ahead as FILL_LOOKAHEADS
 * lookaheads at that level, nearest first, and the next sharper level of `levels` for the first `need` timesteps
 * ahead: the probe that a step up waits for. The probe is spare capacity only:
 * - it exists while the level in use has everything it is asked to hold, so it takes nothing the level in use still needs;
 * - it reaches no further than the lookahead, and only for scrubs of at most PROBE_MAX_NEED timesteps of it.
 * @param {{isReady: (lod: number, t: number) => boolean}} options  `isReady` says whether the complete frame of a level at a timestep is in memory
 * @returns {{primary: {lod: number, steps: number[]}, probe: {lod: number, steps: number[]} | null}}
 */
export function planScrubLoads({ t, direction, count, levels, lod, need, isReady }) {
  const ahead = stepsAhead({ t, direction, count, limit: FILL_LOOKAHEADS * need });
  const full = isReady(lod, t) && readyRun(ahead, (step) => isReady(lod, step)) === ahead.length;
  const sharper = levels[levels.indexOf(lod) - 1];
  return {
    primary: { lod, steps: [t, ...ahead] },
    probe: sharper !== undefined && full && need > 0 && need <= PROBE_MAX_NEED ? { lod: sharper, steps: ahead.slice(0, need) } : null,
  };
}
