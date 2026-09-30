// Movie playback timing, with no DOM and injectable time so it can be tested with a fake clock.
//
// Playback advances one timestep per step interval, forever, looping from the last timestep back to the
// first. The schedule is a fixed grid (each step is due one interval after the previous one was due), so
// timer jitter does not accumulate. If the next timestep is not ready when it is due, playback holds on
// the current one, polling until it is ready, and then re-anchors the grid at that moment so it does not
// fire a burst of steps to catch up.

const DEFAULT_HOLD_POLL_MS = 8;

export class Playback {
  #count;
  #getIndex;
  #goTo;
  #isReady;
  #prepare;
  #now;
  #setTimer;
  #clearTimer;
  #holdPollMs;
  #onChange;
  #stepsPerSecond;
  #playing = false;
  #timer = null;
  #due = 0;
  #lastStepAt = null;
  #holdSince = null;
  #expectedIndex = null;

  /** steps: steps taken; held: steps that were not ready when due; longestHoldMs / totalHoldMs: how long they waited. */
  stats = { steps: 0, held: 0, longestHoldMs: 0, totalHoldMs: 0, firstStepAt: null, lastStepAt: null };

  /**
   * @param {object} options
   * @param {number} options.count              number of timesteps
   * @param {number} options.stepsPerSecond     playback speed, > 0
   * @param {() => number} options.getIndex     the timestep currently shown
   * @param {(index: number, direction: 1) => void} options.goTo  show a timestep; playback always moves forward
   * @param {(index: number) => boolean} options.isReady  whether showing this timestep would be instant
   * @param {(index: number) => void} [options.prepare]   called once when a step has to hold, to start loading it
   * @param {() => void} [options.onChange]     called when playing or the speed changes
   * @param {() => number} [options.now]        clock in ms (default performance.now)
   * @param {(fn: () => void, ms: number) => unknown} [options.setTimer]
   * @param {(handle: unknown) => void} [options.clearTimer]
   * @param {number} [options.holdPollMs]       how often a held step re-checks readiness
   */
  constructor({ count, stepsPerSecond, getIndex, goTo, isReady, prepare, onChange, now, setTimer, clearTimer, holdPollMs = DEFAULT_HOLD_POLL_MS }) {
    if (!Number.isInteger(count) || count < 1) throw new RangeError(`playback needs at least one timestep, got ${count}`);
    this.#count = count;
    this.#getIndex = getIndex;
    this.#goTo = goTo;
    this.#isReady = isReady;
    this.#prepare = prepare;
    this.#onChange = onChange;
    this.#now = now ?? (() => performance.now());
    this.#setTimer = setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.#clearTimer = clearTimer ?? ((handle) => clearTimeout(handle));
    this.#holdPollMs = holdPollMs;
    this.#stepsPerSecond = Playback.#checkSpeed(stepsPerSecond);
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

  get #interval() {
    return 1000 / this.#stepsPerSecond;
  }

  /** Start playing from the timestep on screen: the first step happens now, then one per interval. */
  play() {
    if (this.#playing) return;
    this.#playing = true;
    this.stats = { steps: 0, held: 0, longestHoldMs: 0, totalHoldMs: 0, firstStepAt: null, lastStepAt: null };
    this.#due = this.#now();
    this.#lastStepAt = null;
    this.#holdSince = null;
    this.#expectedIndex = null;
    this.#onChange?.();
    this.#tick();
  }

  /** Stop. Also what a manual scrub calls, so playback never fights the user. */
  pause() {
    if (!this.#playing) return;
    this.#playing = false;
    this.#holdSince = null;
    if (this.#timer !== null) this.#clearTimer(this.#timer);
    this.#timer = null;
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
    const now = this.#now();
    this.#due = (this.#lastStepAt ?? now) + this.#interval;
    this.#reschedule(Math.max(0, this.#due - now));
  }

  #reschedule(ms) {
    if (this.#timer !== null) this.#clearTimer(this.#timer);
    this.#timer = this.#setTimer(() => this.#tick(), ms);
  }

  #tick() {
    this.#timer = null;
    if (!this.#playing) return;
    const index = this.#getIndex();
    if (this.#expectedIndex !== null && index !== this.#expectedIndex) {
      this.pause();
      return;
    }
    const next = (index + 1) % this.#count;
    const now = this.#now();
    if (!this.#isReady(next)) {
      if (this.#holdSince === null) {
        this.#holdSince = Math.min(this.#due, now);
        this.stats.held++;
        this.#prepare?.(next);
      }
      this.#reschedule(this.#holdPollMs);
      return;
    }
    const heldMs = this.#holdSince === null ? 0 : now - this.#holdSince;
    this.stats.longestHoldMs = Math.max(this.stats.longestHoldMs, heldMs);
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
    if (this.#playing) this.#reschedule(Math.max(0, this.#due - this.#now()));
  }
}
