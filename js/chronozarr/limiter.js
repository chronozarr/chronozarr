// Caps concurrent requests to the store. Lower priority numbers start first (0 = a frame is waiting,
// 1 = background prefetch); a queued task whose signal aborts is dropped without running. A background job
// never takes one of the last `reserve` slots, so a demand request always finds a free one.
//
// `priority` is a number or a handle {value}. A handle can be lowered later (a demand request that joins a
// queued background fetch) followed by reprioritize(), which re-sorts the queue.

export class RequestLimiter {
  #max;
  #reserve;
  #active = 0;
  #queue = [];

  constructor(max, { reserve = 0 } = {}) {
    this.#max = max;
    this.#reserve = Math.min(reserve, max - 1);
  }

  get active() {
    return this.#active;
  }

  get queued() {
    return this.#queue.length;
  }

  run(priority, signal, task) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new DOMException('Aborted', 'AbortError'));
        return;
      }
      const handle = typeof priority === 'number' ? { value: priority } : priority;
      const job = { handle, signal, task, resolve, reject };
      job.onAbort = () => {
        const at = this.#queue.indexOf(job);
        if (at < 0) return;
        this.#queue.splice(at, 1);
        signal.removeEventListener('abort', job.onAbort);
        reject(new DOMException('Aborted', 'AbortError'));
        this.#drain();
      };
      signal?.addEventListener('abort', job.onAbort, { once: true });
      const at = this.#queue.findIndex((queued) => queued.handle.value > handle.value);
      if (at < 0) this.#queue.push(job);
      else this.#queue.splice(at, 0, job);
      this.#drain();
    });
  }

  /** Call after lowering a handle's value: queued jobs are re-sorted (stable) and may now start. */
  reprioritize() {
    this.#queue.sort((a, b) => a.handle.value - b.handle.value);
    this.#drain();
  }

  #drain() {
    while (this.#queue.length > 0) {
      const job = this.#queue[0];
      if (job.signal?.aborted) {
        this.#queue.shift();
        job.signal.removeEventListener('abort', job.onAbort);
        job.reject(new DOMException('Aborted', 'AbortError'));
        continue;
      }
      const slots = job.handle.value === 0 ? this.#max : this.#max - this.#reserve;
      if (this.#active >= slots) return;
      this.#queue.shift();
      job.signal?.removeEventListener('abort', job.onAbort);
      this.#active++;
      const finish = () => {
        this.#active--;
        this.#drain();
      };
      let result;
      try { result = job.task(); }
      catch (error) {
        finish();
        job.reject(error);
        continue;
      }
      Promise.resolve(result).then(
        (value) => {
          finish();
          job.resolve(value);
        },
        (error) => {
          finish();
          job.reject(error);
        },
      );
    }
  }
}
