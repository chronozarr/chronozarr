import { test } from 'node:test';
import assert from 'node:assert/strict';
import { windowOrder } from '../chronozarr/decoder.js';
import { GpuWindow } from '../demo/gpu-window.js';

const BASE = { nTime: 20, t: 10, direction: 1, behindFactor: 2, loop: false, steps: 20 };

const fresh = ({ nTime, t, direction, behindFactor, loop, steps }) => windowOrder(nTime, t, { direction, behindFactor, loop }).slice(0, steps);
const ask = (window, { nTime, t, direction, behindFactor, loop, steps }) => window.timesteps(nTime, t, { direction, behindFactor, loop }, steps);

test('the window is the first steps of windowOrder, and asking again with the same inputs gives the same answer without sorting again', () => {
  const window = new GpuWindow();
  const first = ask(window, { ...BASE, steps: 6 });
  assert.deepEqual(first, fresh({ ...BASE, steps: 6 }));
  assert.equal(ask(window, { ...BASE, steps: 6 }), first);

  const sort = Array.prototype.sort;
  let sorts = 0;
  Array.prototype.sort = function (...args) {
    sorts++;
    return sort.apply(this, args);
  };
  try {
    for (let slice = 0; slice < 10; slice++) ask(window, { ...BASE, steps: 6 });
    assert.equal(sorts, 0, 'ten slices, no sort');
    ask(window, { ...BASE, steps: 6, t: 11 });
    assert.equal(sorts, 1, 'a new t, one sort');
  } finally {
    Array.prototype.sort = sort;
  }
});

for (const [input, value] of [
  ['nTime', 12],
  ['t', 4],
  ['direction', -1],
  ['behindFactor', 8],
  ['loop', true],
  ['steps', 5],
]) {
  test(`a change of ${input} is a new window, not the kept one`, () => {
    const changed = { ...BASE, [input]: value };
    assert.notDeepEqual(fresh(changed), fresh(BASE), `${input} must change the window for this test to mean anything`);
    const window = new GpuWindow();
    ask(window, BASE);
    assert.deepEqual(ask(window, changed), fresh(changed));
    assert.deepEqual(ask(window, BASE), fresh(BASE), 'and back');
  });
}
