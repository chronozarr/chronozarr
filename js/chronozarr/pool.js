// Decoding backends behind one interface: decode(bytes, priority, signal) -> Promise<Uint16Array>.
// Lower priority numbers run first (0 = a frame is waiting for it, 1 = background prefetch).

import { createChunkDecoder } from './codec.js';

const abortError = () => new DOMException('Aborted', 'AbortError');

/** Decodes on the calling thread. Used in Node and when workers are disabled. */
export class MainThreadDecoder {
  #decode;

  constructor(zarrita, spec) {
    this.#decode = createChunkDecoder(zarrita, spec);
    this.#decode.catch(() => {});
  }

  async decode(bytes, priority, signal) {
    const decode = await this.#decode;
    if (signal?.aborted) throw abortError();
    return decode(bytes);
  }

  close() {}
}

/**
 * A pool of decode workers. `spawn()` returns a Worker-like object; `init` is the first message
 * each worker gets. Compressed bytes are transferred in, decoded arrays are transferred back, so the
 * caller must not reuse the bytes it passes to decode().
 *
 * Workers take a few hundred milliseconds to load their codecs. With a `fallback` decoder, priority-0
 * jobs that arrive before any worker is ready are decoded on the calling thread instead of waiting, and
 * if the workers cannot start at all (logged once) everything falls back to that decoder.
 */
export class DecodePool {
  #workers;
  #queue = [];
  #jobs = new Map();
  #nextId = 1;
  #failure = null;
  #fallback;

  constructor({ size, spawn, init, fallback = null }) {
    this.#fallback = fallback;
    this.#workers = Array.from({ length: size }, () => {
      const worker = { handle: spawn(), ready: false, busy: false, current: null };
      worker.handle.onmessage = ({ data }) => this.#onMessage(worker, data);
      worker.handle.onerror = (event) => this.#fail(new Error(`decode worker failed: ${event.message ?? event}`));
      worker.handle.postMessage(init);
      return worker;
    });
  }

  get size() {
    return this.#workers.length;
  }

  decode(bytes, priority = 0, signal) {
    if (this.#failure) return Promise.reject(this.#failure);
    if (signal?.aborted) return Promise.reject(abortError());
    if (this.#fallback && priority === 0 && !this.#workers.some((w) => w.ready)) {
      return this.#fallback.decode(bytes, priority, signal);
    }
    return new Promise((resolve, reject) => {
      const job = { id: this.#nextId++, bytes, priority, signal, resolve, reject };
      const at = this.#queue.findIndex((queued) => queued.priority > priority);
      if (at < 0) this.#queue.push(job);
      else this.#queue.splice(at, 0, job);
      this.#dispatch();
    });
  }

  close() {
    for (const worker of this.#workers) worker.handle.terminate();
    this.#fail(abortError());
  }

  #dispatch() {
    for (const worker of this.#workers) {
      if (!worker.ready || worker.busy) continue;
      let job = this.#queue.shift();
      while (job?.signal?.aborted) {
        job.reject(abortError());
        job = this.#queue.shift();
      }
      if (!job) return;
      worker.busy = true;
      worker.current = job.id;
      this.#jobs.set(job.id, job);
      const own = job.bytes.byteOffset === 0 && job.bytes.buffer.byteLength === job.bytes.byteLength;
      const bytes = own ? job.bytes : job.bytes.slice();
      worker.handle.postMessage({ type: 'decode', id: job.id, bytes }, [bytes.buffer]);
    }
  }

  #onMessage(worker, message) {
    if (message.type === 'ready') {
      worker.ready = true;
      this.#dispatch();
    } else if (message.type === 'decoded' || (message.type === 'error' && message.id !== undefined)) {
      const job = this.#jobs.get(message.id);
      this.#jobs.delete(message.id);
      worker.busy = false;
      worker.current = null;
      if (message.type === 'decoded') job.resolve(message.data);
      else job.reject(new Error(message.message));
      this.#dispatch();
    } else if (this.#fallback) {
      this.#useFallback(new Error(`decode worker failed to start: ${message.message}`));
    } else {
      this.#fail(new Error(`decode worker failed to start: ${message.message}`));
    }
  }

  /** The workers cannot run here: hand queued jobs to the fallback decoder and stop using the pool. */
  #useFallback(error) {
    console.warn(`chronozarr: ${error.message}; decoding on the main thread`);
    for (const worker of this.#workers) worker.handle.terminate();
    this.#workers = [];
    const fallback = this.#fallback;
    for (const job of this.#queue.splice(0)) fallback.decode(job.bytes, job.priority, job.signal).then(job.resolve, job.reject);
    this.decode = (bytes, priority, signal) => fallback.decode(bytes, priority, signal);
  }

  #fail(error) {
    this.#failure ??= error;
    for (const job of [...this.#queue, ...this.#jobs.values()]) job.reject(error);
    this.#queue = [];
    this.#jobs.clear();
  }
}
