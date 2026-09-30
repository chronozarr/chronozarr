// Caps concurrent requests to the store. Lower priority numbers start first (0 = a frame is waiting,
// 1 = background prefetch); a queued task whose signal aborts is dropped without running.

export class RequestLimiter {
  #max;
  #active = 0;
  #queue = [];

  constructor(max) {
    this.#max = max;
  }

  get active() {
    return this.#active;
  }

  run(priority, signal, task) {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(new DOMException('Aborted', 'AbortError'));
        return;
      }
      const job = { priority, signal, task, resolve, reject };
      const at = this.#queue.findIndex((queued) => queued.priority > priority);
      if (at < 0) this.#queue.push(job);
      else this.#queue.splice(at, 0, job);
      this.#drain();
    });
  }

  #drain() {
    while (this.#active < this.#max && this.#queue.length > 0) {
      const job = this.#queue.shift();
      if (job.signal?.aborted) {
        job.reject(new DOMException('Aborted', 'AbortError'));
        continue;
      }
      this.#active++;
      const finish = () => {
        this.#active--;
        this.#drain();
      };
      job.task().then(
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
