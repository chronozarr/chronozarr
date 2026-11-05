// The guards of a local benchmark run (support/gate-bench/guards.js): when a machine or a browser is too noisy to measure on,
// and that CI is never refused.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ATTEMPTS, RAF_MAX_MS, frameIntervalProblem, guardsEnabled, guardsMode, guardsSummary, loadLimit, machineProblems, measureGuarded, onBattery, refuseNoisyMachine } from '../support/gate-bench/guards.js';

const BATTERY = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1234567)\t81%; discharging; 6:12 remaining present: true\n";
const AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234567)\t100%; charged; 0:00 remaining present: true\n";

test('pmset says whether the Mac draws from its battery, and nothing when it says nothing', () => {
  assert.equal(onBattery(BATTERY), true);
  assert.equal(onBattery(AC), false);
  assert.equal(onBattery("Now drawing from 'UPS Power'\n"), false);
  assert.equal(onBattery(''), null);
  assert.equal(onBattery('pmset: command not found'), null);
});

test('the load limit is half the logical cores', () => {
  assert.equal(loadLimit(16), 8);
  assert.equal(loadLimit(4), 2);
});

test('a load above half the cores stops a run, and the limit itself does not', () => {
  assert.deepEqual(machineProblems({ load: 8, cores: 16, onBattery: false }), []);
  const problems = machineProblems({ load: 8.1, cores: 16, onBattery: false });
  assert.equal(problems.length, 1);
  assert.match(problems[0], /load average is 8\.1, above half of the 16 logical cores \(8\)/);
});

test('the same load stops a small machine and not a large one', () => {
  assert.equal(machineProblems({ load: 6, cores: 4, onBattery: false }).length, 1);
  assert.deepEqual(machineProblems({ load: 6, cores: 16, onBattery: false }), []);
});

test('battery power stops a run, and an unknown power source does not', () => {
  const problems = machineProblems({ load: 0.5, cores: 16, onBattery: true });
  assert.equal(problems.length, 1);
  assert.match(problems[0], /battery power.*30 Hz/);
  assert.deepEqual(machineProblems({ load: 0.5, cores: 16, onBattery: null }), []);
});

test('a loaded machine on battery power gives both reasons', () => {
  assert.equal(machineProblems({ load: 12, cores: 16, onBattery: true }).length, 2);
});

test('a median frame interval up to 18 ms is fine, a faster display too, and a slower one is refused', () => {
  assert.equal(RAF_MAX_MS, 18);
  for (const ok of [4.2, 8.3, 16.7, 18]) assert.equal(frameIntervalProblem(ok, 'before'), null, `${ok}: 240, 120 and 60 Hz`);
  for (const bad of [18.1, 33.3, Number.NaN, undefined]) assert.match(frameIntervalProblem(bad, 'after'), /interval after the run was .* above 18 ms/, String(bad));
});

test('CI is never guarded, and a local run is guarded unless it says otherwise', () => {
  assert.equal(guardsEnabled({ env: { CI: 'true' } }), false);
  assert.equal(guardsEnabled({ env: {} }), true);
  assert.equal(guardsEnabled({ env: {}, ignore: true }), false);
  assert.equal(guardsEnabled({ env: { CI: '' } }), true);
});

test('the mode says why the guards are off, so that a summary does not look like a guarded one', () => {
  assert.deepEqual(guardsMode({ env: {} }), { enabled: true, off: null });
  assert.deepEqual(guardsMode({ env: { CI: 'true' } }), { enabled: false, off: 'CI' });
  assert.deepEqual(guardsMode({ env: {}, ignore: true }), { enabled: false, off: '--ignore-guards' });
  assert.deepEqual(guardsMode({ env: { CI: 'true' }, ignore: true }), { enabled: false, off: 'CI' });
});

test('the summary line says what was guarded and how many runs were discarded, or that nothing was checked', () => {
  const on = guardsSummary({ mode: guardsMode({ env: {} }), cores: 16, discarded: 2 });
  assert.match(on, /load of at most 8 \(half of 16 logical cores\)/);
  assert.match(on, /at most 18 ms \(2 runs discarded\)/);
  const ignored = guardsSummary({ mode: guardsMode({ env: {}, ignore: true }), cores: 16, discarded: 0 });
  assert.match(ignored, /^Guards: off \(--ignore-guards\): the load, the power source and the animation frame interval were not checked/);
  assert.match(guardsSummary({ mode: guardsMode({ env: { CI: '1' } }), cores: 4, discarded: 0 }), /^Guards: off \(CI\)/);
});

test('a refused machine says why and how to override, and exits; a fit machine goes on', () => {
  const logs = [];
  const exits = [];
  refuseNoisyMachine({ load: 0.5, cores: 16, onBattery: false }, { log: (text) => logs.push(text), exit: (code) => exits.push(code) });
  assert.deepEqual([logs, exits], [[], []]);
  refuseNoisyMachine({ load: 9, cores: 8, onBattery: true }, { log: (text) => logs.push(text), exit: (code) => exits.push(code) });
  assert.deepEqual(exits, [3]);
  assert.match(logs[0], /load average is 9\.0, above half of the 8 logical cores \(4\)/);
  assert.match(logs[0], /battery power/);
  assert.match(logs[0], /--ignore-guards/);
});

/** A measure that returns records with these frame intervals in turn. */
function measures(...intervals) {
  const calls = [];
  const measure = async () => {
    const frameInterval = intervals[calls.length];
    calls.push(frameInterval);
    return { id: calls.length, frameInterval };
  };
  return { measure, calls };
}
const options = (extra = {}) => ({ enabled: true, label: 'pair 2 head run', discarded: [], log: () => {}, ...extra });

test('a run whose browser drew at 60 Hz before and after is kept', async () => {
  const { measure, calls } = measures({ before: 16.7, after: 16.8 });
  const opts = options();
  assert.equal((await measureGuarded(measure, opts)).id, 1);
  assert.equal(calls.length, 1);
  assert.deepEqual(opts.discarded, []);
});

test('a run measured while frames were 33 ms apart is discarded, recorded and measured again', async () => {
  const { measure } = measures({ before: 33.3, after: 16.7 }, { before: 16.7, after: 31 }, { before: 16.7, after: 16.7 });
  const opts = options();
  const record = await measureGuarded(measure, opts);
  assert.equal(record.id, 3);
  assert.equal(opts.discarded.length, 2);
  assert.deepEqual(opts.discarded.map(({ label, attempt }) => [label, attempt]), [['pair 2 head run', 1], ['pair 2 head run', 2]]);
  assert.match(opts.discarded[0].problems[0], /before the run/);
  assert.match(opts.discarded[1].problems[0], /after the run/);
});

test('three discarded attempts in a row stop the job and say how to override', async () => {
  const bad = { before: 33.3, after: 33.3 };
  const { measure, calls } = measures(bad, bad, bad, bad);
  const opts = options();
  await assert.rejects(measureGuarded(measure, opts), new RegExp(`pair 2 head run: ${ATTEMPTS} attempts.*--ignore-guards`));
  assert.equal(calls.length, ATTEMPTS);
  assert.equal(opts.discarded.length, ATTEMPTS);
});

test('with the guards off the first record is kept, whatever its frame intervals', async () => {
  const { measure, calls } = measures({ before: 33.3, after: 33.3 });
  assert.equal((await measureGuarded(measure, options({ enabled: false }))).id, 1);
  assert.equal(calls.length, 1);
});
