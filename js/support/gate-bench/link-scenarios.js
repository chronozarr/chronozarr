// What link.mjs does in the page once the viewer has opened its store and the link is throttled. `page.evaluate` sends the
// source of `runScenario`, so it is one self-contained function, and it drives the viewer as a visitor does: pointer events on
// the timeline, the speed setter and Play, read back through the viewer's `shownFrame`, `probe` and `playback.stats`.
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
  const events = [];
  // A throttled run draws thousands of cells: keep the frames and the holds, the only events the figures use.
  viewer.probe = (event) => {
    if (event.type === 'paint') events.push({ type: 'paint', at: event.at, t: event.t, lod: event.lod, kind: event.kind });
    else if (event.type === 'kept') events.push({ type: 'kept', at: event.at, t: event.t });
  };
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
      const inputs = [];
      const beginAt = performance.now();
      fire(track, 'pointerdown', from);
      for (let t = from + 1; t <= to; t++) {
        const wait = beginAt + (t - from) * spec.stepMs - performance.now();
        if (wait > 0) await sleep(wait);
        inputs.push({ at: performance.now(), t });
        fire(window, 'pointermove', t);
      }
      const releaseAt = performance.now();
      fire(window, 'pointerup', to);
      await waitFor(() => atTarget(to), spec.capMs);
      return { kind: 'drag', events, inputs, releaseAt, normalLod, from, to, capMs: spec.capMs };
    }
    if (spec.kind === 'jump') {
      const to = Math.round((count - 1) / 2);
      const at = performance.now();
      fire(track, 'pointerdown', to);
      await sleep(50);
      fire(window, 'pointerup', to);
      await waitFor(() => atTarget(to), spec.capMs);
      return { kind: 'jump', events, at, to, normalLod, capMs: spec.capMs };
    }
    viewer.setSpeed(spec.stepsPerSecond);
    const startAt = performance.now();
    viewer.play();
    await sleep(spec.windowMs);
    const stats = { ...viewer.playback.stats };
    const movieLod = viewer.movieInfo.lod;
    viewer.pause();
    return { kind: 'play', stats, startAt, windowMs: spec.windowMs, normalLod, movieLod };
  } finally {
    viewer.probe = null;
    delete track.setPointerCapture;
  }
}
