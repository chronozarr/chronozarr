// Aggregate download rate of a store: bytes that arrived, divided by the wall time during which at least one
// request was in flight, both decayed exponentially (time constant TAU_MS). A per-request rate would read
// low by the concurrency factor, and the speculative budget needs what the whole link delivers. Idle time
// counts for nothing, so the estimate is link capacity, not utilisation, and survives pauses.

const TAU_MS = 4000;
const MIN_BYTES = 64 * 1024;
const MIN_BUSY_MS = 1;
/** Seconds of transfer time (not decayed) after which the estimate counts as established. */
const MATURE_BUSY_MS = 1000;

export class BandwidthEstimator {
  #clock;
  #inflight = 0;
  #last = null;
  #bytes = 0;
  #busyMs = 0;
  #totalBusyMs = 0;
  #rate = null;

  constructor(clock = () => performance.now()) {
    this.#clock = clock;
  }

  /** A transfer is starting. */
  begin() {
    this.#advance();
    this.#inflight++;
  }

  /** A transfer finished (or failed, with 0 bytes) after delivering `bytes`. */
  end(bytes) {
    this.#advance();
    this.#inflight--;
    this.#bytes += bytes;
    if (this.#bytes >= MIN_BYTES && this.#busyMs >= MIN_BUSY_MS) this.#rate = (this.#bytes / this.#busyMs) * 1000;
  }

  /** Bytes per second, or null before enough data has arrived to measure. */
  get estimate() {
    return this.#rate;
  }

  /** Whether the estimate rests on at least a second of transfer time: before that it may only reflect a few small, latency-bound requests. */
  get mature() {
    return this.#rate !== null && this.#totalBusyMs >= MATURE_BUSY_MS;
  }

  #advance() {
    const now = this.#clock();
    if (this.#last !== null) {
      const elapsed = Math.max(0, now - this.#last);
      const decay = Math.exp(-elapsed / TAU_MS);
      this.#bytes *= decay;
      this.#busyMs = this.#busyMs * decay + (this.#inflight > 0 ? elapsed : 0);
      if (this.#inflight > 0) this.#totalBusyMs += elapsed;
    }
    this.#last = now;
  }
}
