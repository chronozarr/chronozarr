// Decoding backends behind one interface: decode(bytes, priority, signal, spec) -> Promise<typed array>.
// `spec` = {key, dtype, shape, codecs} describes the chunk (see metadata.js decodeSpec); one backend serves
// several arrays and several stores. Lower priority numbers run first (0 = a frame is waiting for it, 1 =
// background prefetch). `priority` is a number or a handle {value} that can be lowered later; call
// reprioritize() afterwards to re-sort the queue.

import { createChunkDecoder } from './codec.js';

const abortError = () => new DOMException('Aborted', 'AbortError');
const WORKER_IDLE_MS = 30000;

/** Decodes on the calling thread. Used in Node and when workers are disabled. */
export class MainThreadDecoder {
  #zarrita;
  #decoders = new Map();

  constructor(zarrita) {
    this.#zarrita = zarrita;
  }

  decode(bytes, priority, signal, spec) {
    return this.#decode(bytes, signal, spec);
  }

  async #decode(bytes, signal, spec) {
    const decode = await this.#decoderFor(spec);
    if (signal?.aborted) throw abortError();
    return decode(bytes);
  }

  #decoderFor(spec) {
    let decoder = this.#decoders.get(spec.key);
    if (!decoder) {
      decoder = createChunkDecoder(this.#zarrita, spec);
      decoder.catch(() => {});
      this.#decoders.set(spec.key, decoder);
    }
    return decoder;
  }

  /** Instantiate the WASM codecs of `spec` ahead of the first chunk. */
  warm(spec) {
    this.#decoderFor(spec).then((decode) => decode.warmup()).catch(() => {});
  }

  close() {}
}

/**
 * A pool of decode workers. `spawn()` returns a Worker-like object; `init` is the first message each worker
 * gets. Every decode message names the chunk spec, so a worker decodes any array of any store. Compressed
 * bytes are transferred in and decoded arrays transferred back, so the caller must not reuse the bytes it
 * passes to decode().
 *
 * Workers take a few hundred milliseconds to load. With a `fallback` decoder, priority-0 jobs that arrive
 * before any worker is ready are decoded on the calling thread instead of waiting, and if the workers cannot
 * start at all (logged once) everything falls back to that decoder.
 */
export class DecodePool {
  #workers;
  #queue = [];
  #jobs = new Map();
  #nextId = 1;
  #failure = null;
  #fallback;
  #warm = new Map();

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

  /** True once the pool can no longer decode (workers failed and there is no fallback, or it was closed). */
  get failed() {
    return this.#failure !== null;
  }

  /** Have every worker instantiate the codecs for `spec` as soon as it can, so its first real chunk is not slowed. */
  warm(spec) {
    if (this.#warm.has(spec.key)) return;
    this.#warm.set(spec.key, spec);
    for (const worker of this.#workers) if (worker.ready) worker.handle.postMessage({ type: 'warm', spec });
  }

  decode(bytes, priority = 0, signal, spec) {
    if (this.#failure) return Promise.reject(this.#failure);
    if (signal?.aborted) return Promise.reject(abortError());
    const handle = typeof priority === 'number' ? { value: priority } : priority;
    if (this.#fallback && handle.value === 0 && !this.#workers.some((w) => w.ready)) {
      return this.#fallback.decode(bytes, handle, signal, spec);
    }
    return new Promise((resolve, reject) => {
      const job = { id: this.#nextId++, bytes, handle, signal, spec, resolve, reject };
      this.#enqueue(job);
      this.#dispatch();
    });
  }

  /** Call after lowering a priority handle's value: queued jobs are re-sorted (stable) and may now start. */
  reprioritize() {
    this.#queue.sort((a, b) => a.handle.value - b.handle.value);
    this.#dispatch();
  }

  close() {
    for (const worker of this.#workers) worker.handle.terminate();
    this.#fail(abortError());
  }

  #enqueue(job) {
    const at = this.#queue.findIndex((queued) => queued.handle.value > job.handle.value);
    if (at < 0) this.#queue.push(job);
    else this.#queue.splice(at, 0, job);
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
      worker.handle.postMessage({ type: 'decode', id: job.id, spec: job.spec, bytes }, [bytes.buffer]);
    }
  }

  #onMessage(worker, message) {
    if (message.type === 'ready') {
      worker.ready = true;
      for (const spec of this.#warm.values()) worker.handle.postMessage({ type: 'warm', spec });
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
    for (const job of this.#queue.splice(0)) fallback.decode(job.bytes, job.handle, job.signal, job.spec).then(job.resolve, job.reject);
    this.decode = (bytes, priority, signal, spec) => fallback.decode(bytes, priority, signal, spec);
  }

  #fail(error) {
    this.#failure ??= error;
    for (const job of [...this.#queue, ...this.#jobs.values()]) job.reject(error);
    this.#queue = [];
    this.#jobs.clear();
  }
}

const sharedPools = new Map();

/**
 * A lease on a decode pool that outlives the store that asked for it: opening another store right after
 * closing one finds the workers already running with their codecs loaded. Pools are keyed by `key`
 * (worker script and size), shared by every concurrent store, and terminated `idleMs` after the last lease
 * is released. The lease decodes like the pool; close() releases it.
 */
export function leaseDecodePool({ key, size, spawn, init, fallback, idleMs = WORKER_IDLE_MS }) {
  let shared = sharedPools.get(key);
  if (!shared || shared.pool.failed) {
    shared = { pool: new DecodePool({ size, spawn, init, fallback }), leases: 0, timer: null };
    sharedPools.set(key, shared);
  }
  clearTimeout(shared.timer);
  shared.leases++;
  const { pool } = shared;
  let released = false;
  return {
    get size() {
      return pool.size;
    },
    warm: (spec) => pool.warm(spec),
    decode: (bytes, priority, signal, spec) => pool.decode(bytes, priority, signal, spec),
    reprioritize: () => pool.reprioritize(),
    close() {
      if (released) return;
      released = true;
      if (--shared.leases > 0) return;
      shared.timer = setTimeout(() => {
        if (shared.leases === 0 && sharedPools.get(key) === shared) {
          sharedPools.delete(key);
          pool.close();
        }
      }, idleMs);
      shared.timer.unref?.();
    },
  };
}

/** Number of worker pools currently alive (leased or idle, waiting to expire). */
export function sharedPoolCount() {
  return sharedPools.size;
}
