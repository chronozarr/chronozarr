// Guards for a local run of the benchmarks of this directory. A timing taken while other work loads the machine, or while a
// Mac on battery power caps the browser's animation frames at 30 Hz (each paint up to 33 ms late), measures the machine: gate
// runs at a load of 265 to 314 could not establish a gate (docs/evidence.md), and a machine that dropped to 33 ms frames in the
// middle of a matrix went on measuring. So a local run refuses to start when the 1-minute load average is above half the
// logical cores or the machine is on battery power, and discards a run whose animation frame interval, taken on a blank page
// in its browser before and after it, is above RAF_MAX_MS, repeating it up to ATTEMPTS times before it refuses.
// `--ignore-guards` measures anyway, and the summary and the JSON of a run say whether the guards were on.
//
// None of this applies on CI. A shared runner has no battery and a load of other jobs that the job cannot lower, so the guards
// could only fail a job that has no way to pass them; the pairs of compare.js, which put both builds under the same load, are
// what a runner has instead.

import { execFileSync } from 'node:child_process';
import os from 'node:os';

// A load of half the cores is a machine that has the rest to itself: the benchmark's own Chromium and its SwiftShader threads
// are part of the load it measures, and a fixed limit would refuse a 16-core Mac that never falls below 4.
export const loadLimit = (cores) => cores / 2;
// 60 Hz is 16.7 ms, and 18 ms leaves half a frame of slack; a faster frame source adds no noise, so nothing is refused below.
export const RAF_MAX_MS = 18;
export const ATTEMPTS = 3;

/** Whether a run is guarded, and if not, why: `off` is 'CI' or '--ignore-guards'. */
export function guardsMode({ env = process.env, ignore = false } = {}) {
  if (env.CI) return { enabled: false, off: 'CI' };
  if (ignore) return { enabled: false, off: '--ignore-guards' };
  return { enabled: true, off: null };
}

export const guardsEnabled = (options) => guardsMode(options).enabled;

/** The line of a summary that says what was guarded; `discarded` is the number of runs that were discarded. */
export function guardsSummary({ mode, cores, discarded }) {
  if (!mode.enabled) return `Guards: off (${mode.off}): the load, the power source and the animation frame interval were not checked, and no run was discarded for them`;
  return `Guards: a 1-minute load of at most ${loadLimit(cores)} (half of ${cores} logical cores) and no battery power at the start; the median animation frame interval before and after each run at most ${RAF_MAX_MS} ms (${discarded} runs discarded)`;
}

/** Whether `pmset -g batt` says the Mac is drawing from its battery; null when it says nothing about the power source. */
export function onBattery(pmsetOutput) {
  const source = /Now drawing from '([^']+)'/.exec(pmsetOutput)?.[1];
  return source ? source === 'Battery Power' : null;
}

/** The load average, the number of logical cores and the power source of this machine; the power source is null off a Mac or when pmset fails. */
export function readMachine() {
  let battery = null;
  if (process.platform === 'darwin') {
    try {
      battery = onBattery(execFileSync('pmset', ['-g', 'batt'], { encoding: 'utf8' }));
    } catch {
      // Without pmset the power source is unknown, and an unknown source does not stop a run.
    }
  }
  return { load: os.loadavg()[0], cores: os.cpus().length, onBattery: battery };
}

/** Why this machine should not run a benchmark now; empty when it may. */
export function machineProblems({ load, cores, onBattery: battery }) {
  const problems = [];
  if (load > loadLimit(cores)) {
    problems.push(`the 1-minute load average is ${load.toFixed(1)}, above half of the ${cores} logical cores (${loadLimit(cores)}): other work decides the timings (a run's own Chromium adds to the load, so the machine has to be quiet before it starts)`);
  }
  if (battery) problems.push('the Mac is on battery power, which caps animation frames at 30 Hz so that each paint is up to 33 ms late: plug it in');
  return problems;
}

/** Why the animation frames of a browser, with this median interval in ms, are slower than a 60 Hz display's; null when they are not. */
export function frameIntervalProblem(medianMs, when) {
  if (medianMs <= RAF_MAX_MS) return null;
  return `the median animation frame interval ${when} the run was ${Number.isFinite(medianMs) ? medianMs.toFixed(1) : medianMs} ms, above ${RAF_MAX_MS} ms: the browser is not drawing at 60 Hz (battery power, a busy machine or a throttled display)`;
}

/**
 * Refuse to start: print why and exit. Call before anything is measured.
 * @param {{ load: number, cores: number, onBattery: boolean|null }} machine
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
