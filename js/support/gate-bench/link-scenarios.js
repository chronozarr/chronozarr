// What link.mjs does in the page once the viewer has opened its store on the throttled link. `page.evaluate` sends the
// source of `runScenario`, so it is one self-contained function, and it drives the viewer as a visitor does: pointer events on
// the timeline, the speed setter and Play, read back through the viewer's `shownFrame`, `probe` and `playback`.
// The figures are computed from what it returns in link-compare.js, where they can be tested.

/**
 * @param {{kind: 'drag'|'jump'|'play', stepMs?: number, stepsPerSecond?: number, windowMs?: number, capMs: number}} spec
 *   `capMs`: how long a drag or a jump waits for its last frame; `windowMs`: how long playback plays.
 */
export async function runScenario(spec) {
  const { viewer } = window.chronozarr;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const waitFor = async (predicate, capMs) => {
    const started = performance.now();
    while (!predicate()) {
      if (performance.now() - started > capMs) return false;
      await sleep(5);
    }
    return true;
  };
  const shown = viewer.shownFrame;
  if (shown?.kind !== 'target') throw new Error(`the viewer shows no frame at its target level before the scenario starts (shown: ${JSON.stringify(shown)})`);
  const normalLod = shown.lod;
  const count = viewer.store.times.length;
  // What the reader holds and has in flight when the scenario starts: runs that start from different states are not comparable.
  const { requests, bytes, inflight } = viewer.store.stats.network;
  const start = { cache: viewer.store.cacheInfo(), network: { requests, bytes, inflight } };
  // The viewer repaints the frame on screen whenever it has nothing newer (kind 'previous', with the old timestep), and paints
  // the same frame again for every render it is asked for: keep every paint, the figures decide which of them are frames.
  const events = [];
  viewer.probe = (event) => {
    if (event.type === 'paint') events.push({ type: 'paint', at: event.at, t: event.t, lod: event.lod, kind: event.kind });
    else if (event.type === 'kept') events.push({ type: 'kept', at: event.at, t: event.t });
  };
  // At every animation frame: the timestep the viewer was asked for, the one on screen, and whether playback waits for frames.
  // A stall that is open when the scenario ends shows in `stallOpenSince` (playback counts a stall only when it ends).
  const samples = [];
  let stallOpenSince = null;
  let sampling = true;
  const sample = (at) => {
    const frame = viewer.shownFrame;
    samples.push({ at, t: viewer.t, shownT: frame ? frame.t : null });
    const { buffering, stats } = viewer.playback;
    if (buffering && stats.firstStepAt !== null) stallOpenSince ??= at;
    else stallOpenSince = null;
    if (sampling) requestAnimationFrame(sample);
  };
  requestAnimationFrame(sample);
  const track = document.getElementById('timeline-track');
  const fire = (target, type, t) => {
    const rect = track.getBoundingClientRect();
    const x = rect.left + 8 + (t / (count - 1)) * (rect.width - 16);
    target.dispatchEvent(new PointerEvent(type, { clientX: x, clientY: rect.top + rect.height / 2, bubbles: true, pointerId: 1 }));
  };
  // A pointer event that a script dispatches is not from an active pointer, so the viewer's setPointerCapture would throw.
  track.setPointerCapture = () => {};
  const atTarget = (t) => events.some((event) => event.type === 'paint' && event.t === t && event.lod === normalLod && event.kind === 'target');
  try {
    if (spec.kind === 'drag') {
      const from = viewer.t;
      const to = count - 1;
      // `due` is when an input was meant to happen: a main thread that is blocked runs the timer late, and the wait of the
      // input for its frame counts from `due`, so that the stall shows in the lag.
      const inputs = [];
      const beginAt = performance.now();
      fire(track, 'pointerdown', from);
      for (let t = from + 1; t <= to; t++) {
        const due = beginAt + (t - from) * spec.stepMs;
        const wait = due - performance.now();
        if (wait > 0) await sleep(wait);
        inputs.push({ due, at: performance.now(), t });
        fire(window, 'pointermove', t);
      }
      const releaseAt = performance.now();
      fire(window, 'pointerup', to);
      sampling = false;
      await waitFor(() => atTarget(to), spec.capMs);
      return { kind: 'drag', start, events, inputs, samples, beginAt, releaseAt, normalLod, from, to, capMs: spec.capMs };
    }
    if (spec.kind === 'jump') {
      const to = Math.round((count - 1) / 2);
      const at = performance.now();
      fire(track, 'pointerdown', to);
      await sleep(50);
      fire(window, 'pointerup', to);
      await waitFor(() => atTarget(to), spec.capMs);
      return { kind: 'jump', start, events, at, to, normalLod, capMs: spec.capMs };
    }
    viewer.setSpeed(spec.stepsPerSecond);
    const startAt = performance.now();
    viewer.play();
    await sleep(spec.windowMs);
    const endAt = performance.now();
    const stats = { ...viewer.playback.stats };
    const movieLod = viewer.movieInfo.lod;
    viewer.pause();
    return { kind: 'play', start, stats, startAt, endAt, stallOpenSince, windowMs: spec.windowMs, normalLod, movieLod };
  } finally {
    sampling = false;
    viewer.probe = null;
    delete track.setPointerCapture;
  }
}
