// Slot bookkeeping for the GPU texture pool: which chunk lives in which slot, and which slot to give up next.
// No GL in here; the layer uploads into whatever slot this hands out.

export class SlotPool {
  #keyToSlot = new Map();
  #slotKey;
  #lastUsed;
  #free;

  constructor(slots) {
    if (!Number.isInteger(slots) || slots < 1) throw new RangeError(`SlotPool needs at least 1 slot, got ${slots}`);
    this.slots = slots;
    this.#slotKey = new Array(slots).fill(null);
    this.#lastUsed = new Float64Array(slots).fill(-1);
    this.#free = Array.from({ length: slots }, (_, i) => slots - 1 - i);
  }

  get size() {
    return this.#keyToSlot.size;
  }

  /** Slot holding `key`, or -1. With a `frame`, marks the slot as used by that frame (protected from eviction during it). */
  slotOf(key, frame) {
    const slot = this.#keyToSlot.get(key);
    if (slot === undefined) return -1;
    if (frame !== undefined) this.#lastUsed[slot] = frame;
    return slot;
  }

  /**
   * A slot for `key`, used by `frame`. Takes a free slot, else evicts the least recently used slot that no
   * chunk of `frame` is using. Returns {slot, evicted} (evicted: the key that lost its slot, or null), or null
   * when every slot is in use by this frame. Returns the existing slot if `key` is already resident.
   */
  allocate(key, frame) {
    const existing = this.slotOf(key, frame);
    if (existing >= 0) return { slot: existing, evicted: null };
    let slot = this.#free.pop();
    let evicted = null;
    if (slot === undefined) {
      let oldest = Infinity;
      for (let s = 0; s < this.slots; s++) {
        if (this.#lastUsed[s] < frame && this.#lastUsed[s] < oldest) {
          oldest = this.#lastUsed[s];
          slot = s;
        }
      }
      if (slot === undefined) return null;
      evicted = this.#slotKey[slot];
      this.#keyToSlot.delete(evicted);
    }
    this.#slotKey[slot] = key;
    this.#keyToSlot.set(key, slot);
    this.#lastUsed[slot] = frame;
    return { slot, evicted };
  }

  /** Forget everything (the texture keeps its allocation; contents are stale). */
  clear() {
    this.#keyToSlot.clear();
    this.#slotKey.fill(null);
    this.#lastUsed.fill(-1);
    this.#free = Array.from({ length: this.slots }, (_, i) => this.slots - 1 - i);
  }
}
