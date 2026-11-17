import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Machine } from '../chronozarr/machine.js';

// A small lifecycle in the shape the viewer's store session takes: a state may move to itself, and every state accepts
// at least one event except `closed`.
const SESSION = {
  none: { open: 'opening' },
  opening: { open: 'opening', opened: 'open', failed: 'failed' },
  open: { open: 'opening', close: 'closed' },
  failed: { open: 'opening' },
  closed: {},
};

const session = (initial = 'none') => new Machine('viewer session', SESSION, initial);

test('a machine starts in its initial state, and send moves it and returns the new state', () => {
  const machine = session();
  assert.equal(machine.state, 'none');
  assert.equal(machine.send('open'), 'opening');
  assert.equal(machine.state, 'opening');
});

test('a chain of moves follows the table, a move to the same state included', () => {
  const machine = session();
  const visited = ['open', 'open', 'failed', 'open', 'opened', 'close'].map((event) => machine.send(event));
  assert.deepEqual(visited, ['opening', 'opening', 'failed', 'opening', 'open', 'closed']);
  assert.equal(machine.state, 'closed');
});

test('send of an event the state does not accept throws, names the machine, event, state and accepted events, and keeps the state', () => {
  const machine = session('opening');
  assert.throws(
    () => machine.send('close'),
    (error) => {
      assert.ok(error instanceof Error);
      assert.equal(error.message, 'viewer session: "close" is not a move from "opening" (it accepts: open, opened, failed)');
      return true;
    },
  );
  assert.equal(machine.state, 'opening');
  assert.throws(() => session().send('opened'), {
    message: 'viewer session: "opened" is not a move from "none" (it accepts: open)',
  });
});

test('send from a state with no moves says that the state accepts nothing', () => {
  const machine = session('closed');
  assert.throws(() => machine.send('open'), {
    message: 'viewer session: "open" is not a move from "closed" (it accepts nothing)',
  });
  assert.equal(machine.state, 'closed');
});

test('send of an inherited property name is not a move', () => {
  const machine = session();
  assert.throws(() => machine.send('toString'), { message: /"toString" is not a move from "none"/ });
  assert.equal(machine.state, 'none');
});

test('can says whether the current state accepts an event, and an inherited property name is not an event', () => {
  const machine = session('opening');
  assert.equal(machine.can('opened'), true);
  assert.equal(machine.can('open'), true);
  assert.equal(machine.can('close'), false);
  assert.equal(machine.can('toString'), false);
  assert.equal(machine.can('constructor'), false);
  assert.equal(machine.state, 'opening', 'can does not move the machine');
});

test('the constructor refuses a name that is not a non-empty string', () => {
  assert.throws(() => new Machine('', SESSION, 'none'), { message: /name/ });
  assert.throws(() => new Machine(undefined, SESSION, 'none'), { message: /name/ });
  assert.throws(() => new Machine(7, SESSION, 'none'), { message: /name/ });
});

test('the constructor refuses a table that is not a plain object of plain objects, and names the machine', () => {
  for (const table of [null, undefined, [], 'none', new Map([['none', {}]])]) {
    assert.throws(() => new Machine('player', table, 'none'), { message: /^player: / }, String(table));
  }
  assert.throws(() => new Machine('player', { idle: { play: 'playing' }, playing: null }, 'idle'), {
    message: /^player: .*"playing"/,
  });
  assert.throws(() => new Machine('player', { idle: { play: 'playing' }, playing: ['stop'] }, 'idle'), {
    message: /^player: .*"playing"/,
  });
});

test('the constructor refuses a target that is not a state, naming the machine, the state, the event and the target', () => {
  const table = { idle: { play: 'playing' }, playing: { stop: 'idel' } };
  assert.throws(() => new Machine('player', table, 'idle'), (error) => {
    assert.match(error.message, /^player: /);
    assert.match(error.message, /"playing"/);
    assert.match(error.message, /"stop"/);
    assert.match(error.message, /"idel"/);
    return true;
  });
});

test('the constructor refuses an initial state that is not a state of the table, naming the machine and the state', () => {
  assert.throws(() => new Machine('player', { idle: {} }, 'playing'), { message: /^player: .*"playing"/ });
  assert.throws(() => new Machine('player', { idle: {} }, 'toString'), { message: /^player: .*"toString"/ });
});
