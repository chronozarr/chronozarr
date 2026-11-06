// The timesteps the GPU window holds around t, with no DOM.

import { windowOrder } from '../chronozarr/decoder.js';

/**
 * The first `steps` timesteps of the window around `t`, cheapest first (see windowOrder). windowOrder sorts the whole time
 * axis and the fill asks for the window in every slice, so the answer is kept until one of its inputs changes.
 */
export class GpuWindow {
  #key = null;
  #timesteps = [];

  timesteps(nTime, t, { direction, behindFactor, loop }, steps) {
    const key = `${nTime}|${t}|${direction}|${behindFactor}|${loop}|${steps}`;
    if (key !== this.#key) {
      this.#timesteps = windowOrder(nTime, t, { direction, behindFactor, loop }).slice(0, steps);
      this.#key = key;
    }
    return this.#timesteps;
  }
}
