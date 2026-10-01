// Delivery-time model: how long a list of HTTP requests takes over a link of `mbps` megabits per second and `rttMs` of
// round-trip time, with at most `parallel` requests in flight.
//
// A request starts when a slot is free and its dependencies are done, waits `rttMs` for its first byte, then moves its
// bytes over the link, whose capacity is shared equally by every request that is transferring (processor sharing). That
// is the behaviour of one bottleneck link behind HTTP/1.1 with `parallel` connections (6 per origin in a browser) or
// behind HTTP/2 with a client-side cap on streams; it ignores TCP slow start, TLS set-up and server think time.
//
// Requests are `{ id, bytes, after?: [ids] }` in the order they would be issued. `steps` runs lists of requests in order,
// each step starting when the previous one is done (`sequential`, a user waiting for each frame) or all at once
// (pipelined: only the per-request dependencies and `parallel` limit the schedule).

/**
 * @param {{id:string|number, bytes:number, after?:Array<string|number>}[]} requests
 * @param {{rttMs:number, mbps:number, parallel:number}} link  `mbps` may be Infinity
 * @returns {{totalMs:number, finishMs:Map<string|number, number>}}
 */
export function simulate(requests, { rttMs, mbps, parallel }) {
  if (!(parallel >= 1)) throw new RangeError(`parallel must be at least 1, got ${parallel}`);
  const rate = (mbps * 1e6) / 8 / 1000; // bytes per millisecond
  const byId = new Map(requests.map((r) => [r.id, r]));
  for (const r of requests) for (const dep of r.after ?? []) if (!byId.has(dep)) throw new Error(`request ${r.id} waits for unknown request ${dep}`);
  const waiting = new Set(requests.map((r) => r.id));
  const done = new Set();
  const finishMs = new Map();
  const inLatency = new Map(); // id -> time the first byte arrives
  const transferring = new Map(); // id -> bytes left
  let now = 0;

  const startReady = () => {
    for (const r of requests) {
      if (inLatency.size + transferring.size >= parallel) return;
      if (!waiting.has(r.id) || !(r.after ?? []).every((dep) => done.has(dep))) continue;
      waiting.delete(r.id);
      inLatency.set(r.id, now + rttMs);
    }
  };
  const finish = (id) => {
    done.add(id);
    finishMs.set(id, now);
  };

  startReady();
  while (inLatency.size + transferring.size > 0) {
    const share = transferring.size > 0 ? rate / transferring.size : 0;
    let dt = Infinity;
    for (const at of inLatency.values()) dt = Math.min(dt, at - now);
    for (const left of transferring.values()) dt = Math.min(dt, Number.isFinite(share) && share > 0 ? left / share : 0);
    dt = Math.max(0, dt);
    now += dt;
    for (const [id, left] of transferring) transferring.set(id, left - (Number.isFinite(share) ? share * dt : left));
    for (const [id, at] of inLatency) {
      if (at - now > 1e-9) continue;
      inLatency.delete(id);
      transferring.set(id, byId.get(id).bytes);
    }
    for (const [id, left] of [...transferring]) {
      if (left > 1e-6) continue;
      transferring.delete(id);
      finish(id);
    }
    startReady();
  }
  if (waiting.size > 0) throw new Error(`requests never became ready (dependency cycle?): ${[...waiting].slice(0, 5).join(', ')}`);
  return { totalMs: now, finishMs };
}

/**
 * Time for a phase made of steps. `steps[i]` is a list of `{id, bytes, after?}`; `sequential` makes step i+1 wait for
 * every request of step i. Returns the total and, for each step, the time it took from the moment it could start.
 */
export function simulatePhase(steps, link, { sequential }) {
  const flat = [];
  let previous = [];
  steps.forEach((step, i) => {
    const ids = step.map((r) => `${i}:${r.id}`);
    step.forEach((r, k) => {
      flat.push({ id: ids[k], bytes: r.bytes, after: [...(r.after ?? []).map((dep) => `${i}:${dep}`), ...(sequential ? previous : [])] });
    });
    previous = ids;
  });
  const { totalMs, finishMs } = simulate(flat, link);
  const stepMs = steps.map((step, i) => {
    const end = Math.max(0, ...step.map((r) => finishMs.get(`${i}:${r.id}`)));
    const start = sequential && i > 0 ? Math.max(0, ...steps[i - 1].map((r) => finishMs.get(`${i - 1}:${r.id}`))) : 0;
    return end - start;
  });
  return { totalMs, stepMs };
}
