// How fast the user is scrubbing the timeline, and the level that speed asks for, with no DOM and injectable time.
//
// A scrub is a run of manual steps (a drag on the timeline, held or repeated arrow keys). Its speed is the distance
// covered per second over the last SCRUB_WINDOW_MS. 300 ms holds three or more steps at 7 steps/s and still follows
// a change of pace within a third of a second. The speed is measured from one step to the latest instead of counting
// steps per window, so a steady drag reads the same whichever way its steps fall against the window edges.
// With no step for SCRUB_WINDOW_MS the scrub is over and the speed is 0.

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
 * The level to draw at while scrubbing at `speed` timesteps per second: the sharpest level from the normal one
 * `baseLod` down to `deepestLod` (the level of a view four times further out, never coarser) for which
 * `linkOk(lod, speed)` says the link can feed the scrub. The normal level when the user is not scrubbing (speed 0),
 * when it passes itself, and when no level does: a coarser level that cannot be fed either only costs resolution.
 * It is the movie rule (chooseMovieLevel) without the memory test, which a scrub does not need, and without its floor
 * (a movie plays at the deepest level whatever the link says; a scrub that nothing can feed stays as it is).
 * @param {{baseLod: number, deepestLod: number, speed: number, linkOk: (lod: number, speed: number) => boolean}} options
 */
export function chooseScrubLevel({ baseLod, deepestLod, speed, linkOk }) {
  if (!(speed > 0)) return baseLod;
  for (let lod = baseLod; lod <= deepestLod; lod++) if (linkOk(lod, speed)) return lod;
  return baseLod;
}

/** How far ahead of the timestep on screen the link has to keep up: the link check compares bytes per second, so the next second of the scrub. */
export const LOOKAHEAD_SECONDS = 1;

/** ...but not past the reader's idle prefetch horizon (12 timesteps either side of t, decoder.js): how much of the scrub the viewer loads ahead of it. */
export const LOOKAHEAD_MAX_STEPS = 12;

/**
 * The timesteps a scrub at `speed` timesteps per second reaches in the next LOOKAHEAD_SECONDS (at most
 * LOOKAHEAD_MAX_STEPS), nearest first, going the way it is going from timestep `t` and stopping at the ends of the
 * axis. They are what the link has to deliver for the scrub: only the cold ones cost it anything, so a scrub
 * through timesteps that are in memory already needs no more of it than a pause does.
 */
export function scrubAhead({ t, direction, speed, count }) {
  const ahead = [];
  for (let step = 1; step <= Math.min(Math.ceil(speed * LOOKAHEAD_SECONDS), LOOKAHEAD_MAX_STEPS); step++) {
    const next = t + direction * step;
    if (next < 0 || next >= count) break;
    ahead.push(next);
  }
  return ahead;
}
