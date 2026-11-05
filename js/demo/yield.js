// Giving the main thread back between the slices of a long job.

const waiting = [];
let channel = null;

/** Run the next job of a message queue: one message, one job. */
function runNext() {
  waiting.shift()();
}

function postMessageTask(job) {
  if (channel === null) {
    channel = new MessageChannel();
    channel.port1.onmessage = runNext;
    // Node, which runs the tests, does not exit while a port listens; browsers have no unref.
    channel.port1.unref?.();
  }
  waiting.push(job);
  channel.port2.postMessage(null);
}

/**
 * Run `task` in a task of its own, after the ones that are waiting, and return a function that cancels it while it is
 * still pending. A job that continues itself with setTimeout(0) waits at least 4 ms between slices once the timers are
 * nested five deep (the HTML spec); a message does not wait, nor does scheduler.postTask, which also puts the task
 * behind decode results and input (background priority) where the browser has it.
 */
export function yieldToMain(task) {
  let pending = true;
  const run = () => {
    if (!pending) return;
    pending = false;
    task();
  };
  if (typeof globalThis.scheduler?.postTask === 'function') globalThis.scheduler.postTask(run, { priority: 'background' });
  else postMessageTask(run);
  return () => {
    pending = false;
  };
}
