// Giving the main thread back between the slices of a long job.

const waiting = [];
let channel = null;

function postMessageTask(task) {
  if (channel === null) {
    channel = new MessageChannel();
    // A message that finds the queue empty belongs to a task that was cancelled: nothing to run.
    channel.port1.onmessage = () => waiting.shift()?.();
    // Node, which runs the tests, does not exit while a port listens; browsers have no unref.
    channel.port1.unref?.();
  }
  waiting.push(task);
  channel.port2.postMessage(null);
  return () => {
    const index = waiting.indexOf(task);
    if (index >= 0) waiting.splice(index, 1);
  };
}

function postSchedulerTask(task) {
  const controller = new AbortController();
  globalThis.scheduler.postTask(task, { signal: controller.signal }).catch((error) => {
    if (error?.name === 'AbortError') return;
    // The task threw: report it as an uncaught error, as a timer or a message would, not as a rejection nobody handles.
    queueMicrotask(() => {
      throw error;
    });
  });
  return () => controller.abort();
}

/**
 * Run `task` in a task of its own, after the ones that are waiting, and return a function that cancels it while it is
 * still pending (a cancelled task leaves the queue). A job that continues itself with setTimeout(0) waits at least 4 ms
 * between slices once the timers are nested five deep (the HTML spec); a message does not wait, nor does
 * scheduler.postTask at its default priority, which is used where the browser has it.
 */
export function yieldToMain(task) {
  return typeof globalThis.scheduler?.postTask === 'function' ? postSchedulerTask(task) : postMessageTask(task);
}
