// Movie playback timing and buffering, with no DOM and injectable time so it can be tested with a fake frame clock.
//
// Playback is driven by animation frames. On each frame it steps to the next timestep if that step is due,
// looping from the last timestep back to the first. The schedule is a fixed grid (each step is due one
// interval after the previous one was due), so frame jitter does not accumulate; a step is taken on the
// frame nearest to its due time. At most one step happens per frame, so the effective rate is capped at
// the display refresh rate, which is measured from the frame timestamps while playing.
//
// It buffers like a video player. Before it starts it waits until the next START_BUFFER_SECONDS of frames are
// ready (the whole loop when that is shorter); while it plays it asks for more whenever fewer than
// LOW_BUFFER_SECONDS are ready ahead; and if the next frame is not ready when it is due it pauses, asking for
// the next START_BUFFER_SECONDS, and starts again by itself once they are in. It never holds on a single frame
// and never skips one. After a pause the schedule restarts from that moment, so it does not burst to catch up.

/** Selectable speeds, steps per second: every integer up to 15, then coarser up to 60. */
export const SPEEDS = [...Array.from({ length: 15 }, (_, i) => i + 1), 20, 24, 30, 40, 48, 60];
export const DEFAULT_STEPS_PER_SECOND = 4;

/** Seconds of frames that must be ready before playback starts or resumes. */
export const START_BUFFER_SECONDS = 2;
/** Playback asks for more frames when fewer than this many seconds are ready ahead. */
export const LOW_BUFFER_SECONDS = 1;

/** The selectable speed closest to `value` (for saved or hand-set values). */
export function snapSpeed(value) {
  return SPEEDS.reduce((best, speed) => (Math.abs(speed - value) < Math.abs(best - value) ? speed : best), SPEEDS[0]);
}

const DEFAULT_FRAME_MS = 1000 / 60;
const MIN_REFRESH_SAMPLES = 8;
const MAX_REFRESH_SAMPLES = 30;
const FILL_REQUEST_MS = 250;

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
  #wrap;
  #startSeconds;
  #lowSeconds;
  #stepsPerSecond;
  #playing = false;
  #buffering = false;
  #bufferingSince = null;
  #bufferingWraps = false;
  #restart = false;
  #playStartedAt = 0;
  #lastFillAt = -Infinity;
  #frame = null;
  #frameDeltas = [];
  #lastFrameAt = null;
  #due = 0;
  #lastStepAt = null;
  #expectedIndex = null;
  #reportedEffective = null;
  #reportedAhead = null;

  /**
   * steps: steps taken. bufferingPauses: times it ran out of frames after it had started (wrapPauses: the ones at
   * the loop's wrap, from the last timestep to the first); bufferingMs / longestBufferingMs: how long those took.
   * initialBufferMs: from play() to the first step.
   */
  stats = Playback.#emptyStats();

  /**
   * @param {object} options
   * @param {number} options.count              number of timesteps
   * @param {number} options.stepsPerSecond     requested speed, > 0
   * @param {() => number} options.getIndex     the timestep currently shown
   * @param {(index: number, direction: 1) => void} options.goTo  show a timestep; playback always moves forward
   * @param {(index: number) => boolean} options.isReady  whether showing this timestep would be instant
   * @param {(indices: number[]) => void} [options.prepare]  asks for these timesteps (the next ones, nearest first) to be loaded; called on entering
   *   a buffering pause, while one lasts about every 250 ms, and when fewer than a second of frames are ready ahead
   * @param {() => void} [options.onChange]     called when playing, buffering, the buffered count, the speed or the effective speed changes
   * @param {boolean} [options.wrap=true]       loop from the last timestep to the first; false stops playback at the last timestep
   * @param {number} [options.startSeconds]     frames to have ready before starting or resuming (default START_BUFFER_SECONDS)
   * @param {number} [options.lowSeconds]       when fewer than this are ready ahead, ask for more (default LOW_BUFFER_SECONDS)
   * @param {() => number} [options.now]        clock in ms, in the same time base as frame timestamps (default performance.now)
   * @param {(callback: (frameTime: number) => void) => unknown} [options.requestFrame]  default requestAnimationFrame
   * @param {(handle: unknown) => void} [options.cancelFrame]
   */
  constructor({
    count,
    stepsPerSecond,
    getIndex,
    goTo,
    isReady,
    prepare,
    onChange,
    wrap = true,
    startSeconds = START_BUFFER_SECONDS,
    lowSeconds = LOW_BUFFER_SECONDS,
    now,
    requestFrame,
    cancelFrame,
  }) {
    if (!Number.isInteger(count) || count < 1) throw new RangeError(`playback needs at least one timestep, got ${count}`);
    this.#count = count;
    this.#getIndex = getIndex;
    this.#goTo = goTo;
    this.#isReady = isReady;
    this.#prepare = prepare;
    this.#onChange = onChange;
    this.#wrap = wrap;
    this.#startSeconds = startSeconds;
    this.#lowSeconds = lowSeconds;
    this.#now = now ?? (() => performance.now());
    this.#requestFrame = requestFrame ?? ((callback) => requestAnimationFrame(callback));
    this.#cancelFrame = cancelFrame ?? ((handle) => cancelAnimationFrame(handle));
    this.#stepsPerSecond = Playback.#checkSpeed(stepsPerSecond);
  }

  static #emptyStats() {
    return { steps: 0, bufferingPauses: 0, wrapPauses: 0, bufferingMs: 0, longestBufferingMs: 0, initialBufferMs: null, firstStepAt: null, lastStepAt: null };
  }

  static #checkSpeed(stepsPerSecond) {
    if (!Number.isFinite(stepsPerSecond) || stepsPerSecond <= 0) throw new RangeError(`steps per second must be positive, got ${stepsPerSecond}`);
    return stepsPerSecond;
  }

  /** Whether playback is on: true while it buffers too (the frames it will play are being loaded), false after pause(). */
  get playing() {
    return this.#playing;
  }

  /** Whether playback is on but waiting for frames: before the first step, or after it ran out. */
  get buffering() {
    return this.#buffering;
  }

  get stepsPerSecond() {
    return this.#stepsPerSecond;
  }

  /** Frames ready ahead of the one on screen, and how many it wants before it (re)starts. */
  get buffered() {
    const needed = this.#needed(this.#startSeconds);
    return { ahead: this.#readyAhead(needed), needed };
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

  #next(index) {
    return (index + 1) % this.#count;
  }

  /** How many frames `seconds` of playback is, at least one, and no more than there are ahead to show. */
  #needed(seconds) {
    const room = this.#wrap ? this.#count - 1 : this.#count - 1 - this.#getIndex();
    return Math.max(0, Math.min(room, Math.ceil(seconds * this.effectiveStepsPerSecond)));
  }

  /** The number of consecutive ready frames after the one on screen, up to `limit`. */
  #readyAhead(limit) {
    const index = this.#getIndex();
    let ready = 0;
    while (ready < limit && this.#isReady((index + ready + 1) % this.#count)) ready++;
    return ready;
  }

  /** The next frames to have ready, nearest first. */
  #wanted() {
    const index = this.#getIndex();
    return Array.from({ length: this.#needed(this.#startSeconds) }, (_, k) => (index + k + 1) % this.#count);
  }

  /** Ask for the frames to be loaded, at most every FILL_REQUEST_MS unless `force`. */
  #requestFill(now, force = false) {
    if (!force && now - this.#lastFillAt < FILL_REQUEST_MS) return;
    this.#lastFillAt = now;
    this.#prepare?.(this.#wanted());
  }

  /** Start playing from the timestep on screen once enough frames are ready: the first step happens at once, then one per interval. */
  play() {
    if (this.#playing) return;
    this.#playing = true;
    this.stats = Playback.#emptyStats();
    this.#lastStepAt = null;
    this.#expectedIndex = null;
    this.#frameDeltas = [];
    this.#lastFrameAt = null;
    this.#reportedEffective = null;
    this.#reportedAhead = null;
    this.#restart = false;
    this.#playStartedAt = this.#now();
    this.#due = this.#playStartedAt;
    const needed = this.#needed(this.#startSeconds);
    if (this.#readyAhead(needed) >= needed) {
      this.#buffering = false;
      this.#onChange?.();
      this.#step(this.#due);
    } else {
      this.#buffering = true;
      this.#bufferingSince = this.#playStartedAt;
      this.#bufferingWraps = false;
      this.#onChange?.();
      this.#requestFill(this.#playStartedAt, true);
    }
    if (this.#playing) this.#queueFrame();
  }

  /** Stop. Also what a manual scrub calls, so playback never fights the user. */
  pause() {
    if (!this.#playing) return;
    this.#playing = false;
    this.#buffering = false;
    this.#bufferingSince = null;
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
    if (!this.#playing || this.#buffering) return;
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
    if (this.#expectedIndex !== null && this.#getIndex() !== this.#expectedIndex) {
      this.pause();
      return;
    }
    if (this.#buffering) this.#whileBuffering(frameTime);
    else {
      const low = this.#needed(this.#lowSeconds);
      if (this.#readyAhead(low) < low) this.#requestFill(frameTime);
      this.#step(frameTime);
    }
    if (this.#playing) {
      this.#queueFrame();
      const effective = Math.round(this.effectiveStepsPerSecond);
      if (effective !== this.#reportedEffective) {
        this.#reportedEffective = effective;
        this.#onChange?.();
      }
    }
  }

  /** Waiting for frames: resume when the start buffer is full, else keep asking and tell the UI how far it is. */
  #whileBuffering(now) {
    const needed = this.#needed(this.#startSeconds);
    const ahead = this.#readyAhead(needed);
    if (ahead >= needed) {
      const waited = now - this.#bufferingSince;
      if (this.stats.firstStepAt === null) this.stats.initialBufferMs = now - this.#playStartedAt;
      else {
        this.stats.bufferingMs += waited;
        this.stats.longestBufferingMs = Math.max(this.stats.longestBufferingMs, waited);
      }
      this.#buffering = false;
      this.#bufferingSince = null;
      this.#restart = true;
      this.#due = now;
      this.#onChange?.();
      this.#step(now);
      return;
    }
    this.#requestFill(now);
    if (ahead !== this.#reportedAhead) {
      this.#reportedAhead = ahead;
      this.#onChange?.();
    }
  }

  #step(now) {
    const index = this.#getIndex();
    if (this.#expectedIndex !== null && index !== this.#expectedIndex) {
      this.pause();
      return;
    }
    if (this.#due - now > this.frameMs / 2) return;
    if (!this.#wrap && index >= this.#count - 1) {
      this.pause();
      return;
    }
    const next = this.#next(index);
    if (!this.#isReady(next)) {
      this.#runDry(now, next);
      return;
    }
    const late = now - this.#due;
    const interval = this.#interval;
    const restart = this.#restart;
    this.#restart = false;
    this.#expectedIndex = next;
    this.#goTo(next, 1);
    if (this.stats.firstStepAt === null && this.stats.initialBufferMs === null) this.stats.initialBufferMs = now - this.#playStartedAt;
    this.stats.steps++;
    this.stats.firstStepAt ??= now;
    this.stats.lastStepAt = now;
    this.#lastStepAt = now;
    this.#due = restart || late > interval ? now + interval : this.#due + interval;
  }

  /** The next frame is not ready when it is due: stop and fill the buffer again instead of holding on one frame. */
  #runDry(now, next) {
    this.#buffering = true;
    this.#bufferingSince = now;
    this.#reportedAhead = null;
    this.stats.bufferingPauses++;
    if (next === 0 && this.#count > 1) this.stats.wrapPauses++;
    this.#onChange?.();
    this.#requestFill(now, true);
  }
}

/** A cold loop may use this share of the measured bandwidth; the rest is headroom for other traffic and for the estimate being off. */
export const LINK_HEADROOM = 0.7;

/** Compressed bytes per decoded byte assumed until the caches say otherwise (Sentinel-2 composites compress to 0.6 to 0.9). */
export const DEFAULT_WIRE_RATIO = 0.8;
const MIN_RATIO_SAMPLE_BYTES = 8 * 1024 * 1024;

/** Bytes on the wire per decoded byte: what the caches hold, compressed over decoded, once there is enough of it to tell. */
export function wireRatio({ compressedBytes, decodedBytes }) {
  if (compressedBytes === null || decodedBytes === null || decodedBytes < MIN_RATIO_SAMPLE_BYTES) return DEFAULT_WIRE_RATIO;
  return Math.min(1.2, Math.max(0.2, compressedBytes / decodedBytes));
}

/**
 * Whether a movie at `stepsPerSecond` can be fed by the link: the bytes of one step, times the speed, times the
 * share of the loop that is not in memory yet (`coldFraction`, 0 to 1), must stay under LINK_HEADROOM of the
 * measured `bandwidth` (bytes per second). Yes when nothing has to be fetched or no bandwidth has been measured.
 */
export function linkAllows({ bytesPerStep, stepsPerSecond, coldFraction, bandwidth, headroom = LINK_HEADROOM }) {
  if (bandwidth === null || bandwidth === undefined || !(bandwidth > 0) || coldFraction <= 0) return true;
  return bytesPerStep * stepsPerSecond * coldFraction < headroom * bandwidth;
}

const REASON_LABELS = { memory: 'fits memory', link: 'link', 'memory+link': 'fits memory + link' };

/** The hint text for a `reason` from chooseMovieLevel. */
export function describeReason(reason) {
  return REASON_LABELS[reason] ?? '';
}

/**
 * The level a movie plays at, and why. The normal level `baseLod` if the whole loop for the visible cells fits
 * the decoded cache there (`fits(lod)`) and, for a loop that still has to be fetched, the link can feed it
 * (`linkOk(lod)`); otherwise the first coarser level where both hold, and never coarser than `deepestLod` (the
 * level the viewer would pick four times further out): if nothing does, that one. `reason` is null at the normal
 * level, else 'memory', 'link' or 'memory+link' for the rules that ruled out the levels passed over.
 * @returns {{lod: number, reason: null | 'memory' | 'link' | 'memory+link'}}
 */
export function chooseMovieLevel({ baseLod, deepestLod, fits, linkOk = () => true }) {
  const floor = Math.max(baseLod, deepestLod);
  const failed = new Set();
  const reasonOf = () => (failed.size === 0 ? null : [...failed].sort().reverse().join('+'));
  for (let lod = baseLod; lod <= floor; lod++) {
    const memory = fits(lod);
    const link = linkOk(lod);
    if (memory && link) return { lod, reason: lod === baseLod ? null : reasonOf() };
    if (!memory) failed.add('memory');
    if (!link) failed.add('link');
  }
  return { lod: floor, reason: floor === baseLod ? null : reasonOf() };
}
