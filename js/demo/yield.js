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
 * Run `task` in a task of its own after the next animation frame has been painted, and return a function that cancels it
 * while it is still pending. A job that continues itself this way runs one slice per frame and none between frames.
 */
export function yieldAfterPaint(task) {
  let cancelTask = null;
  const frame = requestAnimationFrame(() => {
    cancelTask = yieldToMain(task);
  });
  return () => {
    cancelAnimationFrame(frame);
    cancelTask?.();
  };
}
