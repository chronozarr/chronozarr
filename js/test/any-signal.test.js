import { test } from 'node:test';
import assert from 'node:assert/strict';
import { getEventListeners } from 'node:events';
import { anySignal } from '../chronozarr/http.js';
import { openStore } from '../chronozarr/decoder.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

// anySignal is AbortSignal.any with a fallback for browsers that lack it (Chrome before 116, Firefox before 124, Safari before 17.4).

/** Run `body` with the global AbortSignal.any removed, as in an older browser. */
async function withoutNativeAny(body) {
  const native = AbortSignal.any;
  delete AbortSignal.any;
  try {
    assert.equal(AbortSignal.any, undefined);
    await body();
  } finally {
    AbortSignal.any = native;
  }
}

const settled = () => new Promise((resolve) => setImmediate(resolve));

for (const mode of ['with AbortSignal.any', 'without AbortSignal.any']) {
  const run = mode === 'with AbortSignal.any' ? (body) => body() : withoutNativeAny;

  test(`anySignal ${mode}: aborts with the reason of the first input to abort, and not before`, () =>
    run(() => {
      const [a, b, c] = [new AbortController(), new AbortController(), new AbortController()];
      const any = anySignal([a.signal, b.signal, c.signal]);
      assert.equal(any.aborted, false);
      b.abort(new Error('b went first'));
      assert.equal(any.aborted, true);
      assert.equal(any.reason.message, 'b went first');
      a.abort(new Error('a came late'));
      assert.equal(any.reason.message, 'b went first', 'the reason does not change');
    }));

  test(`anySignal ${mode}: an input that is already aborted aborts it at once, the first of them giving the reason`, () =>
    run(() => {
      const live = new AbortController();
      const early = AbortSignal.abort(new Error('early'));
      const late = AbortSignal.abort(new Error('late'));
      const any = anySignal([live.signal, early, late]);
      assert.equal(any.aborted, true);
      assert.equal(any.reason.message, 'early');
    }));

  test(`anySignal ${mode}: tells listeners of its own abort event, once`, () =>
    run(() => {
      const [a, b] = [new AbortController(), new AbortController()];
      const any = anySignal([a.signal, b.signal]);
      let heard = 0;
      any.addEventListener('abort', () => heard++);
      a.abort();
      b.abort();
      assert.equal(heard, 1);
    }));
}

test('anySignal without AbortSignal.any takes its listeners off the other inputs once it has aborted', () =>
  withoutNativeAny(() => {
    const [a, b, c] = [new AbortController(), new AbortController(), new AbortController()];
    const any = anySignal([a.signal, b.signal, c.signal]);
    for (const { signal } of [a, b, c]) assert.equal(getEventListeners(signal, 'abort').length, 1, 'one listener on each input while none has aborted');
    c.abort();
    assert.equal(any.aborted, true);
    for (const { signal } of [a, b]) assert.equal(getEventListeners(signal, 'abort').length, 0, 'gone from the inputs that did not abort');
  }));

test('anySignal without AbortSignal.any leaves no listener on an input when one is already aborted', () =>
  withoutNativeAny(() => {
    const live = new AbortController();
    anySignal([AbortSignal.abort(), live.signal]);
    assert.equal(getEventListeners(live.signal, 'abort').length, 0);
  }));

test('prefetch cancels its fetches on the caller\'s signal and on close() without AbortSignal.any', () =>
  withoutNativeAny(async () => {
    const readable = buildSyntheticStore({ nTime: 20, nBand: 1, height: 32, width: 32, chunk: 32, sharded: false, delayMs: 40 });
    const store = await openStore('memory://old-browser', { store: readable, workers: 0, speculativeBytesInitial: 1e12 });
    const abort = new AbortController();
    const job = store.prefetch({ lod: 0, cells: [[0, 0]], t: 10, concurrency: 2, signal: abort.signal });
    await settled();
    abort.abort();
    const aborted = await job;
    assert.ok(aborted.fetched < aborted.planned, 'the aborted job stopped');
    const second = store.prefetch({ lod: 0, cells: [[0, 0]], t: 10, concurrency: 2 });
    await new Promise((resolve) => setTimeout(resolve, 20));
    store.close();
    const closed = await second;
    assert.ok(closed.fetched < closed.planned, 'close() stopped a job that had no signal of its own');
  }));
