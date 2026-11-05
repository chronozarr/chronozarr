// Guards for a local run of the benchmarks of this directory. A timing taken while other work loads the machine, or while a
// Mac on battery power caps the browser's animation frames at 30 Hz (each paint up to 33 ms late), measures the machine: gate
// runs at a load of 265 to 314 could not establish a gate (docs/evidence.md), and a machine that dropped to 33 ms frames in the
// middle of a matrix went on measuring. So a local run refuses to start when the 1-minute load average is above LOAD_LIMIT or
// the machine is on battery power, and discards a run whose animation frame interval, taken on a blank page in its browser
// before and after it, is outside RAF_RANGE_MS, repeating it up to ATTEMPTS times before it refuses. `--ignore-guards` measures anyway.
//
// None of this applies on CI. A shared runner has no battery and a load of other jobs that the job cannot lower, so the guards
// could only fail a job that has no way to pass them; the pairs of compare.js, which put both builds under the same load, are
// what a runner has instead.

import { execFileSync } from 'node:child_process';
import os from 'node:os';

export const LOAD_LIMIT = 4;
// 60 Hz is 16.7 ms; the lower bound is a frame early by a timer's resolution, the upper bound a frame late by half a frame's slack.
export const RAF_RANGE_MS = [15.5, 18];
export const ATTEMPTS = 3;

export const guardsEnabled = ({ env = process.env, ignore = false } = {}) => !env.CI && !ignore;

/** Whether `pmset -g batt` says the Mac is drawing from its battery; null when it says nothing about the power source. */
export function onBattery(pmsetOutput) {
  const source = /Now drawing from '([^']+)'/.exec(pmsetOutput)?.[1];
  return source ? source === 'Battery Power' : null;
}

/** The load average and the power source of this machine; the power source is null off a Mac or when pmset fails. */
export function readMachine() {
  let battery = null;
  if (process.platform === 'darwin') {
    try {
      battery = onBattery(execFileSync('pmset', ['-g', 'batt'], { encoding: 'utf8' }));
    } catch {
      // Without pmset the power source is unknown, and an unknown source does not stop a run.
    }
  }
  return { load: os.loadavg()[0], onBattery: battery };
}

/** Why this machine should not run a benchmark now; empty when it may. */
export function machineProblems({ load, onBattery: battery }) {
  const problems = [];
  if (load > LOAD_LIMIT) {
    problems.push(`the 1-minute load average is ${load.toFixed(1)}, above ${LOAD_LIMIT}: other work decides the timings (a run's own Chromium adds to the load, so the machine has to be quiet before it starts)`);
  }
  if (battery) problems.push('the Mac is on battery power, which caps animation frames at 30 Hz so that each paint is up to 33 ms late: plug it in');
  return problems;
}

/** Why the animation frames of a browser, with this median interval in ms, are not those of a 60 Hz display; null when they are. */
export function frameIntervalProblem(medianMs, when) {
  const [low, high] = RAF_RANGE_MS;
  if (medianMs >= low && medianMs <= high) return null;
  return `the median animation frame interval ${when} the run was ${Number.isFinite(medianMs) ? medianMs.toFixed(1) : medianMs} ms, outside ${low} to ${high} ms: the browser is not drawing at 60 Hz (battery power, a busy machine or a throttled display)`;
}

/**
 * Refuse to start: print why and exit. Call before anything is measured.
 * @param {{ load: number, onBattery: boolean|null }} machine
 */
export function refuseNoisyMachine(machine, { log = console.error, exit = process.exit } = {}) {
  const problems = machineProblems(machine);
  if (!problems.length) return;
  log(`Refusing to measure on this machine:\n${problems.map((problem) => `- ${problem}`).join('\n')}\nRun with --ignore-guards to measure anyway; the figures will be noise.`);
  exit(3);
}

/**
 * Run a measurement until its frame intervals are in range. `measure` resolves with a record that has
 * `frameInterval: { before, after }` (the median in ms, from the page); a record outside the range is discarded, with its reasons
 * pushed to `discarded`, and measured again, up to ATTEMPTS times in all. With the guards off, the first record is returned.
 */
export async function measureGuarded(measure, { enabled, label, discarded, log = console.warn }) {
  for (let attempt = 1; ; attempt++) {
    const record = await measure();
    if (!enabled) return record;
    const problems = [frameIntervalProblem(record.frameInterval.before, 'before'), frameIntervalProblem(record.frameInterval.after, 'after')].filter(Boolean);
    if (!problems.length) return record;
    discarded.push({ label, attempt, problems });
    log(`discarded ${label}, attempt ${attempt} of ${ATTEMPTS}: ${problems.join('; ')}`);
    if (attempt === ATTEMPTS) {
      throw new Error(`${label}: ${ATTEMPTS} attempts all ran on a browser that was not drawing at 60 Hz (${problems[0]}). Run with --ignore-guards to measure anyway; the figures will be noise.`);
    }
  }
}
