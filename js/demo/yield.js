// Giving the main thread back between the slices of a long job.

const waiting = [];
let channel = null;

/**
 * Run `task` in a task of its own, after the ones that are waiting, and return a function that cancels it while it is
 * still pending (a cancelled task leaves the queue). A job that continues itself with setTimeout(0) waits at least 4 ms
 * between slices once the timers are nested five deep (the HTML spec); a message does not wait.
 */
export function yieldToMain(task) {
  if (channel === null) {
    channel = new MessageChannel();
    // A message that finds the queue empty belongs to a task that was cancelled: nothing to run.
    channel.port1.onmessage = () => waiting.shift()?.task();
    // Node, which runs the tests, does not exit while a port listens; browsers have no unref.
    channel.port1.unref?.();
  }
  // The canceller removes this entry, not whichever entry holds the same function.
  const entry = { task };
  waiting.push(entry);
  channel.port2.postMessage(null);
  return () => {
    const index = waiting.indexOf(entry);
    if (index >= 0) waiting.splice(index, 1);
  };
}

/**
 * A job of short slices that waits for its turn between them. `slice()` does one slice and says whether work is left;
 * `isPaced()` says whether frames are being drawn one after the other, and `budgetMs()` how long after the start of a
 * frame the job may go on working.
 *
 * While frames follow one another the job waits for the paint of the next frame and runs a slice, and goes on in tasks of
 * its own until `budgetMs()` have passed since the start of that frame: input and the next frame wait behind one slice
 * at most, and nothing runs between the budget and the next frame. The first slice after a paint runs whatever the paint
 * cost, so a long paint cannot starve the job. When nothing moves the slices follow each other with no frame in between.
 * `schedule()` while a slice is waiting changes nothing: the slice reads the state when it runs.
 */
export class SliceScheduler {
  #slice;
  #isPaced;
  #budgetMs;
  #now;
  #pending = null;

  constructor({ slice, isPaced, budgetMs, now = () => performance.now() }) {
    this.#slice = slice;
    this.#isPaced = isPaced;
    this.#budgetMs = budgetMs;
    this.#now = now;
  }

  /** Have the job run, in the way that suits the moment, unless a slice is waiting in that way already. */
  schedule() {
    const paced = this.#isPaced();
    if (this.#pending && (this.#pending.kind !== 'unpaced') === paced) return;
    this.cancel();
    if (paced) {
      const frame = requestAnimationFrame((frameStart) => this.#queuePacedSlice(frameStart, false));
      this.#pending = { kind: 'frame', cancel: () => cancelAnimationFrame(frame) };
    } else {
      this.#pending = { kind: 'unpaced', cancel: yieldToMain(() => this.#runUnpaced()) };
    }
  }

  cancel() {
    this.#pending?.cancel();
    this.#pending = null;
  }

  /** Drop a slice already queued as a task. A slice still waiting for a frame can use the new view once it paints. */
  cancelQueuedTask() {
    if (this.#pending?.kind !== 'frame') this.cancel();
  }

  #runUnpaced() {
    this.#pending = null;
    if (this.#slice()) this.schedule();
  }

  #queuePacedSlice(frameStart, continuing) {
    this.#pending = { kind: 'paced', cancel: yieldToMain(() => this.#runPaced(frameStart, continuing)) };
  }

  #runPaced(frameStart, continuing) {
    this.#pending = null;
    if (continuing && this.#overBudget(frameStart)) {
      this.schedule();
      return;
    }
    if (!this.#slice()) return;
    if (this.#isPaced() && !this.#overBudget(frameStart)) this.#queuePacedSlice(frameStart, true);
    else this.schedule();
  }

  #overBudget(frameStart) {
    return this.#now() - frameStart >= this.#budgetMs();
  }
}
