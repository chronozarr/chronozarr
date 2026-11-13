// Decoding backends behind one interface: decode(bytes, priority, signal, spec, options) -> Promise<typed array>.
// With options.compact, uint16 output may instead be lossless GPU-readable backing with unpack().
// `spec` = {key, dtype, shape, codecs} describes the chunk (see metadata.js decodeSpec); one backend serves
// several arrays and several stores. Lower priority numbers run first (0 = a frame is waiting for it, 1 =
// background prefetch). `priority` is a number or a handle {value} that can be lowered later; call
// reprioritize() afterwards to re-sort the queue.

import { createChunkDecoder } from './codec.js';
import { packUint16, restoreCompactUint16 } from './compact.js';
import { Machine } from './machine.js';

const abortError = () => new DOMException('Aborted', 'AbortError');
const WORKER_IDLE_MS = 30000;

/** Decodes on the calling thread. Used in Node and when workers are disabled. */
export class MainThreadDecoder {
  #zarrita;
  #decoders = new Map();
  #closed = false;

  constructor(zarrita) {
    this.#zarrita = zarrita;
  }

  decode(bytes, priority, signal, spec, options = {}) {
    return this.#decode(bytes, signal, spec, options);
  }

  async #decode(bytes, signal, spec, options) {
    const decode = await this.#decoderFor(spec);
    if (signal?.aborted) throw abortError();
    const data = await decode(bytes);
    return options.compact ? packUint16(data) ?? data : data;
  }

  #decoderFor(spec) {
    if (this.#closed) throw abortError();
    const { key } = spec;
    let decoder = this.#decoders.get(key);
    if (!decoder) {
      decoder = createChunkDecoder(this.#zarrita, spec);
      // A load that failed (a codec fetch that hit a network error) is forgotten, so the next use loads again. The
      // handler also keeps a failed warm-up from being reported as an unhandled rejection.
      decoder.catch(() => {
        if (this.#decoders.get(key) === decoder) this.#decoders.delete(key);
      });
      this.#decoders.set(key, decoder);
    }
    return decoder;
  }

  /** Instantiate the WASM codecs of `spec` ahead of the first chunk. */
  warm(spec) {
    this.#decoderFor(spec).then((decode) => decode.warmup()).catch(() => {});
  }

  close() {
    this.#closed = true;
    this.#decoders.clear();
  }
}

/**
 * The modes of a DecodePool. starting: no worker is ready yet. running: at least one is. fallback: the workers could
 * not be used and the fallback decoder does the work. failed: the workers could not be used and there is no fallback.
 * closed: close() was called.
 */
const POOL_MOVES = {
  starting: { ready: 'running', fallback: 'fallback', fail: 'failed', close: 'closed' },
  running: { fallback: 'fallback', fail: 'failed', close: 'closed' },
  fallback: { close: 'closed' },
  failed: {},
  closed: {},
};

/**
 * A pool of decode workers. `spawn()` returns a Worker-like object; `init` is the first message each worker
 * gets. Every decode message names the chunk spec, so a worker decodes any array of any store. Compressed
 * bytes are transferred in and decoded arrays transferred back, so the caller must not reuse the bytes it
 * passes to decode().
 *
 * Workers take a few hundred milliseconds to load. With a `fallback` decoder, priority-0 jobs that arrive
 * before any worker is ready are decoded on the calling thread instead of waiting, and if a worker cannot start
 * or stops with an error (logged once) everything falls back to that decoder; the jobs a worker held at that
 * moment are rejected. Without a fallback the pool fails and rejects every job.
 */
export class DecodePool {
  #mode = new Machine('decode pool', POOL_MOVES, 'starting');
  #workers;
  #queue = [];
  #jobs = new Map();
  #nextId = 1;
  /** The Error that a failed or closed pool rejects every decode with. */
  #failure = null;
  #fallback;
  #warm = new Map();

  /** Throws what `spawn()` throws, after terminating the workers it had already started. */
  constructor({ size, spawn, init, fallback = null }) {
    this.#fallback = fallback;
    this.#workers = [];
    try {
      for (let i = 0; i < size; i++) {
        const worker = { handle: spawn(), ready: false, busy: false, current: null };
        this.#workers.push(worker);
        worker.handle.onmessage = ({ data }) => this.#onMessage(worker, data);
        worker.handle.onerror = (event) => this.#workersFailed(new Error(`decode worker failed: ${event.message ?? event}`));
        worker.handle.postMessage(init);
      }
    } catch (error) {
      for (const worker of this.#workers) worker.handle.terminate();
      throw error;
    }
  }

  get size() {
    return this.#workers.length;
  }

  /** One of starting, running, fallback, failed, closed (see POOL_MOVES). */
  get mode() {
    return this.#mode.state;
  }

  /** True once the pool can no longer decode (workers failed and there is no fallback, or it was closed). */
  get failed() {
    return this.#mode.state === 'failed' || this.#mode.state === 'closed';
  }

  /**
   * Have every worker, and the fallback decoder, instantiate the codecs for `spec` as soon as they can, so the first real
   * chunk is not slowed. The fallback decodes whatever arrives before a worker is ready: its codecs load while the chunk
   * downloads instead of after it.
   */
  warm(spec) {
    if (this.#warm.has(spec.key)) return;
    this.#warm.set(spec.key, spec);
    this.#fallback?.warm?.(spec);
    for (const worker of this.#workers) if (worker.ready) worker.handle.postMessage({ type: 'warm', spec });
  }

  decode(bytes, priority = 0, signal, spec, options = {}) {
    const mode = this.#mode.state;
    if (mode === 'fallback') return this.#fallback.decode(bytes, priority, signal, spec, options);
    if (mode === 'failed' || mode === 'closed') return Promise.reject(this.#failure);
    if (signal?.aborted) return Promise.reject(abortError());
    const handle = typeof priority === 'number' ? { value: priority } : priority;
    if (this.#fallback && handle.value === 0 && mode === 'starting') {
      return this.#fallback.decode(bytes, handle, signal, spec, options);
    }
    return new Promise((resolve, reject) => {
      const job = { id: this.#nextId++, bytes, handle, signal, spec, options, resolve, reject };
      job.onAbort = () => {
        const at = this.#queue.indexOf(job);
        if (at >= 0) this.#queue.splice(at, 1);
        this.#jobs.delete(job.id);
        signal.removeEventListener('abort', job.onAbort);
        job.reject(abortError());
      };
      signal?.addEventListener('abort', job.onAbort, { once: true });
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
    if (!this.#mode.can('close')) return;
    this.#fail('close', abortError());
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
        job.signal.removeEventListener('abort', job.onAbort);
        job.reject(abortError());
        job = this.#queue.shift();
      }
      if (!job) return;
      worker.busy = true;
      worker.current = job.id;
      this.#jobs.set(job.id, job);
      const own = job.bytes.byteOffset === 0 && job.bytes.buffer.byteLength === job.bytes.byteLength;
      const bytes = own ? job.bytes : job.bytes.slice();
      worker.handle.postMessage({ type: 'decode', id: job.id, spec: job.spec, bytes, compact: !!job.options.compact }, [bytes.buffer]);
    }
  }

  #onMessage(worker, message) {
    const mode = this.#mode.state;
    if (mode !== 'running' && mode !== 'starting') return;
    if (message.type === 'ready') {
      if (mode === 'starting') this.#mode.send('ready');
      worker.ready = true;
      for (const spec of this.#warm.values()) worker.handle.postMessage({ type: 'warm', spec });
      this.#dispatch();
    } else if (message.type === 'decoded' || (message.type === 'error' && message.id !== undefined)) {
      if (worker.current !== message.id) return;
      const job = this.#jobs.get(message.id);
      this.#jobs.delete(message.id);
      worker.busy = false;
      worker.current = null;
      if (job) {
        job.signal?.removeEventListener('abort', job.onAbort);
        if (message.type === 'decoded') job.resolve(message.data?.words ? restoreCompactUint16(message.data) : message.data);
        else job.reject(new Error(message.message));
      }
      this.#dispatch();
    } else {
      this.#workersFailed(new Error(`decode worker failed to start: ${message.message}`));
    }
  }

  /** A worker could not start or stopped with an error: use the fallback if there is one, else fail. */
  #workersFailed(error) {
    const mode = this.#mode.state;
    if (mode !== 'starting' && mode !== 'running') return;
    if (this.#fallback) this.#useFallback(error);
    else this.#fail('fail', error);
  }

  /**
   * The workers cannot run here: stop them, hand queued jobs to the fallback decoder and decode everything with it
   * from now on. Jobs that were on a worker are rejected: their bytes were transferred to it and cannot be decoded here.
   */
  #useFallback(error) {
    this.#mode.send('fallback');
    console.warn(`chronozarr: ${error.message}; decoding on the main thread`);
    this.#stopWorkers();
    const stopped = new Error(`decode worker stopped before it answered (${error.message}); its chunk bytes went to the worker, read the chunk again`);
    for (const job of this.#jobs.values()) {
      job.signal?.removeEventListener('abort', job.onAbort);
      job.reject(stopped);
    }
    this.#jobs.clear();
    const fallback = this.#fallback;
    for (const job of this.#queue.splice(0)) {
      job.signal?.removeEventListener('abort', job.onAbort);
      fallback.decode(job.bytes, job.handle, job.signal, job.spec, job.options).then(job.resolve, job.reject);
    }
  }

  #stopWorkers() {
    for (const worker of this.#workers) {
      worker.handle.terminate();
      worker.handle.onmessage = null;
      worker.handle.onerror = null;
    }
    this.#workers = [];
  }

  /** Move to failed (`event` 'fail') or closed ('close'): stop the workers and the fallback, reject every job with `error`. */
  #fail(event, error) {
    this.#mode.send(event);
    this.#failure = error;
    this.#stopWorkers();
    this.#warm.clear();
    this.#fallback?.close?.();
    for (const job of [...this.#queue, ...this.#jobs.values()]) {
      job.signal?.removeEventListener('abort', job.onAbort);
      job.reject(error);
    }
    this.#queue = [];
    this.#jobs.clear();
  }
}

const sharedPools = new Map();

/**
 * A lease on a decode pool that outlives the store that asked for it: opening another store right after
 * closing one finds the workers already running with their codecs loaded. Pools are keyed by `key`
 * (worker script and size), shared by every concurrent store, and terminated `idleMs` after the last lease
 * is released. A pool that is no longer starting or running (it fell back, failed or was closed) is not handed
 * out again: the next lease starts a new one. The lease decodes like the pool; close() releases it.
 */
export function leaseDecodePool({ key, size, spawn, init, fallback, idleMs = WORKER_IDLE_MS }) {
  let shared = sharedPools.get(key);
  const mode = shared?.pool.mode;
  if (mode !== 'starting' && mode !== 'running') {
    // A pool that has no usable workers is not shared any more. One that nobody holds is closed now; one that is
    // still leased is closed when its last lease is released.
    if (shared && shared.leases === 0) {
      clearTimeout(shared.timer);
      shared.pool.close();
    }
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
    decode: (bytes, priority, signal, spec, options) => pool.decode(bytes, priority, signal, spec, options),
    reprioritize: () => pool.reprioritize(),
    close() {
      if (released) return;
      released = true;
      if (--shared.leases > 0) return;
      if (sharedPools.get(key) !== shared) {
        pool.close();
        return;
      }
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
