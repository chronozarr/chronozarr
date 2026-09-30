// Movie playback timing, with no DOM and injectable time so it can be tested with a fake frame clock.
//
// Playback is driven by animation frames. On each frame it steps to the next timestep if that step is due,
// looping from the last timestep back to the first. The schedule is a fixed grid (each step is due one
// interval after the previous one was due), so frame jitter does not accumulate; a step is taken on the
// frame nearest to its due time. At most one step happens per frame, so the effective rate is capped at
// the display refresh rate, which is measured from the frame timestamps while playing.
//
// If the next timestep is not ready when it is due, playback holds on the current one, checking again each
// frame, and then re-anchors the grid at that moment so it never fires a burst of steps to catch up.

/** Selectable speeds, steps per second: every integer up to 15, then coarser up to 60. */
export const SPEEDS = [...Array.from({ length: 15 }, (_, i) => i + 1), 20, 24, 30, 40, 48, 60];
export const DEFAULT_STEPS_PER_SECOND = 4;

/** The selectable speed closest to `value` (for saved or hand-set values). */
export function snapSpeed(value) {
  return SPEEDS.reduce((best, speed) => (Math.abs(speed - value) < Math.abs(best - value) ? speed : best), SPEEDS[0]);
}

const DEFAULT_FRAME_MS = 1000 / 60;
const MIN_REFRESH_SAMPLES = 8;
const MAX_REFRESH_SAMPLES = 30;

export class Playback {
  #count;
  #getIndex;
  #goTo;
  #isReady;
  #prepare;
  #now;
  #requestFrame;
  #cancelFrame;
  #onChange;
  #stepsPerSecond;
  #playing = false;
  #frame = null;
  #frameDeltas = [];
  #lastFrameAt = null;
  #due = 0;
  #lastStepAt = null;
  #holdSince = null;
  #holdIsWrap = false;
  #expectedIndex = null;
  #reportedEffective = null;

  /**
   * steps: steps taken; held: steps that were not ready when due (wrapHeld: the ones at the loop's wrap, from the
   * last timestep to the first); longestHoldMs / longestWrapHoldMs / totalHoldMs: how long they waited.
   */
  stats = Playback.#emptyStats();

  /**
   * @param {object} options
   * @param {number} options.count              number of timesteps
   * @param {number} options.stepsPerSecond     requested speed, > 0
   * @param {() => number} options.getIndex     the timestep currently shown
   * @param {(index: number, direction: 1) => void} options.goTo  show a timestep; playback always moves forward
   * @param {(index: number) => boolean} options.isReady  whether showing this timestep would be instant
   * @param {(index: number) => void} [options.prepare]   called once when a step has to hold, to start loading it
   * @param {() => void} [options.onChange]     called when playing, the speed or the effective speed changes
   * @param {() => number} [options.now]        clock in ms, in the same time base as frame timestamps (default performance.now)
   * @param {(callback: (frameTime: number) => void) => unknown} [options.requestFrame]  default requestAnimationFrame
   * @param {(handle: unknown) => void} [options.cancelFrame]
   */
  constructor({ count, stepsPerSecond, getIndex, goTo, isReady, prepare, onChange, now, requestFrame, cancelFrame }) {
    if (!Number.isInteger(count) || count < 1) throw new RangeError(`playback needs at least one timestep, got ${count}`);
    this.#count = count;
    this.#getIndex = getIndex;
    this.#goTo = goTo;
    this.#isReady = isReady;
    this.#prepare = prepare;
    this.#onChange = onChange;
    this.#now = now ?? (() => performance.now());
    this.#requestFrame = requestFrame ?? ((callback) => requestAnimationFrame(callback));
    this.#cancelFrame = cancelFrame ?? ((handle) => cancelAnimationFrame(handle));
    this.#stepsPerSecond = Playback.#checkSpeed(stepsPerSecond);
  }

  static #emptyStats() {
    return { steps: 0, held: 0, wrapHeld: 0, longestHoldMs: 0, longestWrapHoldMs: 0, totalHoldMs: 0, firstStepAt: null, lastStepAt: null };
  }

  static #checkSpeed(stepsPerSecond) {
    if (!Number.isFinite(stepsPerSecond) || stepsPerSecond <= 0) throw new RangeError(`steps per second must be positive, got ${stepsPerSecond}`);
    return stepsPerSecond;
  }

  get playing() {
    return this.#playing;
  }

  get stepsPerSecond() {
    return this.#stepsPerSecond;
  }

  /** Display refresh rate in Hz, measured from frame timestamps while playing; null until enough frames were seen. */
  get refreshHz() {
    const frameMs = this.#measuredFrameMs();
    return frameMs === null ? null : 1000 / frameMs;
  }

  /** Length of one display frame in ms: measured, or 60 Hz until measured. */
  get frameMs() {
    return this.#measuredFrameMs() ?? DEFAULT_FRAME_MS;
  }

  /** The speed playback can actually deliver: the requested speed, capped at the display refresh rate. */
  get effectiveStepsPerSecond() {
    const refreshHz = this.refreshHz;
    return refreshHz === null ? this.#stepsPerSecond : Math.min(this.#stepsPerSecond, refreshHz);
  }

  get #interval() {
    return 1000 / this.effectiveStepsPerSecond;
  }

  #measuredFrameMs() {
    if (this.#frameDeltas.length < MIN_REFRESH_SAMPLES) return null;
    const sorted = [...this.#frameDeltas].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)];
  }

  /** Start playing from the timestep on screen: the first step happens now, then one per interval. */
  play() {
    if (this.#playing) return;
    this.#playing = true;
    this.stats = Playback.#emptyStats();
    this.#due = this.#now();
    this.#lastStepAt = null;
    this.#holdSince = null;
    this.#expectedIndex = null;
    this.#frameDeltas = [];
    this.#lastFrameAt = null;
    this.#reportedEffective = null;
    this.#onChange?.();
    this.#step(this.#due);
    if (this.#playing) this.#queueFrame();
  }

  /** Stop. Also what a manual scrub calls, so playback never fights the user. */
  pause() {
    if (!this.#playing) return;
    this.#playing = false;
    this.#holdSince = null;
    if (this.#frame !== null) this.#cancelFrame(this.#frame);
    this.#frame = null;
    this.#onChange?.();
  }

  toggle() {
    if (this.#playing) this.pause();
    else this.play();
  }

  /** Change speed, also while playing: the next step is one new interval after the last one. */
  setSpeed(stepsPerSecond) {
    this.#stepsPerSecond = Playback.#checkSpeed(stepsPerSecond);
    this.#onChange?.();
    if (!this.#playing || this.#holdSince !== null) return;
    this.#due = (this.#lastStepAt ?? this.#now()) + this.#interval;
  }

  #queueFrame() {
    this.#frame = this.#requestFrame((frameTime) => this.#onFrame(frameTime));
  }

  #onFrame(frameTime) {
    this.#frame = null;
    if (!this.#playing) return;
    if (this.#lastFrameAt !== null) {
      this.#frameDeltas.push(frameTime - this.#lastFrameAt);
      if (this.#frameDeltas.length > MAX_REFRESH_SAMPLES) this.#frameDeltas.shift();
    }
    this.#lastFrameAt = frameTime;
    this.#step(frameTime);
    if (this.#playing) {
      this.#queueFrame();
      const effective = Math.round(this.effectiveStepsPerSecond);
      if (effective !== this.#reportedEffective) {
        this.#reportedEffective = effective;
        this.#onChange?.();
      }
    }
  }

  #step(now) {
    const index = this.#getIndex();
    if (this.#expectedIndex !== null && index !== this.#expectedIndex) {
      this.pause();
      return;
    }
    if (this.#holdSince === null && this.#due - now > this.frameMs / 2) return;
    const next = (index + 1) % this.#count;
    if (!this.#isReady(next)) {
      if (this.#holdSince === null) {
        this.#holdSince = Math.min(this.#due, now);
        this.#holdIsWrap = next === 0 && this.#count > 1;
        this.stats.held++;
        if (this.#holdIsWrap) this.stats.wrapHeld++;
        this.#prepare?.(next);
      }
      return;
    }
    const heldMs = this.#holdSince === null ? 0 : now - this.#holdSince;
    this.stats.longestHoldMs = Math.max(this.stats.longestHoldMs, heldMs);
    if (this.#holdIsWrap) this.stats.longestWrapHoldMs = Math.max(this.stats.longestWrapHoldMs, heldMs);
    this.#holdIsWrap = false;
    this.stats.totalHoldMs += heldMs;
    const late = now - this.#due;
    const interval = this.#interval;
    const afterHold = this.#holdSince !== null;
    this.#holdSince = null;
    this.#expectedIndex = next;
    this.#goTo(next, 1);
    this.stats.steps++;
    this.stats.firstStepAt ??= now;
    this.stats.lastStepAt = now;
    this.#lastStepAt = now;
    this.#due = afterHold || late > interval ? now + interval : this.#due + interval;
  }
}
