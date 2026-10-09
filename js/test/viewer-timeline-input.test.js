import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { bindTimelineInput } from '../demo/input.js';

const MOUSE = 1;
const FINGER_A = 2;
const FINGER_B = 3;

/** The 117-month timeline track at x 100..1116 (8 px of padding each side); `viewer` records the calls the gesture makes. */
function setup() {
  const track = new EventTarget();
  const captured = [];
  track.setPointerCapture = (id) => captured.push(id);
  track.getBoundingClientRect = () => ({ left: 100, width: 1016 });
  const win = new EventTarget();
  const viewer = {
    store: { times: { length: 117 } },
    calls: [],
    goToTime(t) {
      this.calls.push(['go', t]);
    },
    endScrub() {
      this.calls.push(['end']);
    },
  };
  globalThis.document = { getElementById: (id) => (id === 'timeline-track' ? track : null) };
  globalThis.window = win;
  bindTimelineInput(viewer);
  const pointer = (target, type, pointerId, clientX) => {
    const event = Object.assign(new Event(type, { cancelable: true }), { pointerId, clientX });
    target.dispatchEvent(event);
    return event;
  };
  const xOf = (t) => 100 + 8 + (t / 116) * (1016 - 16);
  return {
    viewer,
    captured,
    xOf,
    down: (id, t) => pointer(track, 'pointerdown', id, xOf(t)),
    move: (id, x) => pointer(win, 'pointermove', id, x),
    up: (id) => pointer(win, 'pointerup', id, 0),
    cancel: (id) => pointer(win, 'pointercancel', id, 0),
  };
}

afterEach(() => {
  delete globalThis.document;
  delete globalThis.window;
});

test('timeline: a press scrubs to the month under it, a move follows, a release ends the scrub', () => {
  const { viewer, down, move, up, xOf } = setup();
  down(MOUSE, 10);
  move(MOUSE, xOf(11));
  move(MOUSE, xOf(12));
  up(MOUSE);
  assert.deepEqual(viewer.calls, [['go', 10], ['go', 11], ['go', 12], ['end']]);
});

test('timeline: the press captures its pointer and does not start a text selection', () => {
  const { captured, down } = setup();
  const press = down(FINGER_A, 10);
  assert.deepEqual(captured, [FINGER_A]);
  assert.equal(press.defaultPrevented, true);
});

test('timeline: moves with nothing pressed, or after the release, do nothing', () => {
  const { viewer, down, move, up, xOf } = setup();
  move(MOUSE, xOf(30));
  down(MOUSE, 10);
  up(MOUSE);
  move(MOUSE, xOf(40));
  assert.deepEqual(viewer.calls, [['go', 10], ['end']]);
});

test('timeline: a second finger panning the map does not move the scrub of the first', () => {
  const { viewer, down, move, xOf } = setup();
  down(FINGER_A, 18);
  move(FINGER_A, xOf(19));
  // The map is under the second finger; its x, read as a place on the track, would be month 42.
  for (const t of [42, 43, 44]) move(FINGER_B, xOf(t));
  move(FINGER_A, xOf(20));
  assert.deepEqual(viewer.calls, [['go', 18], ['go', 19], ['go', 20]]);
});

test('timeline: the second finger leaving does not end the scrub of the first', () => {
  const { viewer, down, move, up, cancel, xOf } = setup();
  down(FINGER_A, 18);
  up(FINGER_B);
  cancel(FINGER_B);
  move(FINGER_A, xOf(19));
  assert.deepEqual(viewer.calls, [['go', 18], ['go', 19]], 'no endScrub, and the first finger still scrubs');
  up(FINGER_A);
  assert.deepEqual(viewer.calls.at(-1), ['end']);
});

test('timeline: the scrub is the pointer that pressed the track last, so a press that never ended cannot lock it', () => {
  const { viewer, down, move, up, xOf } = setup();
  down(FINGER_A, 10);
  down(FINGER_B, 50);
  move(FINGER_A, xOf(11));
  move(FINGER_B, xOf(51));
  up(FINGER_A);
  up(FINGER_B);
  assert.deepEqual(viewer.calls, [['go', 10], ['go', 50], ['go', 51], ['end']], 'the first finger is no longer the scrub: its move and release are ignored');
});

test('timeline: a cancelled pointer ends the scrub of that pointer', () => {
  const { viewer, down, cancel, move, xOf } = setup();
  down(FINGER_A, 10);
  cancel(FINGER_A);
  move(FINGER_A, xOf(20));
  assert.deepEqual(viewer.calls, [['go', 10], ['end']]);
});

test('timeline: without a store the press scrubs nothing', () => {
  const { viewer, down, move, up, xOf } = setup();
  viewer.store = null;
  down(MOUSE, 10);
  move(MOUSE, xOf(11));
  up(MOUSE);
  assert.deepEqual(viewer.calls, []);
});
