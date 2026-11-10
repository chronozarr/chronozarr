// What the link runner counts on the wire (support/gate-bench/cdp.js): the log of a page's requests from CDP's events, and the
// throttling it applies. A fake session stands in for the browser and plays the events of a real request in order.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { NetworkLog, attachNetworkLog, setLink } from '../support/gate-bench/cdp.js';

const ORIGIN = 'http://127.0.0.1:5000';

class FakeSession {
  handlers = new Map();
  sent = [];

  on(name, handler) {
    this.handlers.set(name, handler);
  }

  emit(name, event) {
    this.handlers.get(name)?.(event);
  }

  async send(method, params) {
    this.sent.push([method, params]);
  }

  /** The events of a request as CDP orders them: sent, body chunks, then finished (with the bytes on the wire, headers included). */
  start(id, url, timestamp, type = 'Fetch') {
    this.emit('Network.requestWillBeSent', { requestId: id, request: { url }, timestamp, type });
  }

  data(id, bytes) {
    this.emit('Network.dataReceived', { requestId: id, encodedDataLength: bytes });
  }

  finish(id, timestamp, encodedDataLength) {
    this.emit('Network.loadingFinished', { requestId: id, timestamp, encodedDataLength });
  }

  fail(id, timestamp) {
    this.emit('Network.loadingFailed', { requestId: id, timestamp });
  }
}

const setup = (origins = [ORIGIN]) => {
  const session = new FakeSession();
  return { session, log: new NetworkLog(session, origins) };
};

test('a request that finishes counts the bytes on the wire that CDP reports for it, headers included', () => {
  const { session, log } = setup();
  session.start('a', `${ORIGIN}/0/data/c/0/0/0/0`, 10);
  session.data('a', 600_000);
  session.data('a', 700_000);
  assert.deepEqual(log.snapshot(), { requests: 1, bytes: 1_300_000, failed: 0 }, 'the body so far while it runs');
  session.finish('a', 10.4, 1_300_412);
  assert.deepEqual(log.snapshot(), { requests: 1, bytes: 1_300_412, failed: 0 }, 'the total of the finished request replaces the running sum, not adds to it');
  assert.deepEqual(log.entries().map(({ startedAt, endedAt }) => [startedAt, endedAt]), [[10, 10.4]]);
});

test('a request that is cancelled keeps the body bytes it had received and counts as failed', () => {
  const { session, log } = setup();
  session.start('a', `${ORIGIN}/0/data/c/1/0/0/0`, 10);
  session.data('a', 250_000);
  session.fail('a', 10.2);
  assert.deepEqual(log.snapshot(), { requests: 1, bytes: 250_000, failed: 1 });
  assert.equal(log.entries()[0].failed, true);
});

test('only requests to the origins it was given are logged, and an origin is not a prefix of another port', () => {
  const { session, log } = setup([ORIGIN]);
  session.start('page', 'http://127.0.0.1:5001/demo/viewer.js', 10);
  session.start('lookalike', 'http://127.0.0.1:50001/0/data/c/0/0/0/0', 10);
  session.start('store', `${ORIGIN}/zarr.json`, 10);
  session.data('page', 5);
  session.data('lookalike', 7);
  session.finish('lookalike', 11, 7);
  assert.deepEqual(log.snapshot(), { requests: 1, bytes: 0, failed: 0 });
  assert.deepEqual(log.entries().map((entry) => entry.url), [`${ORIGIN}/zarr.json`]);
});

test('an origin given with its slash matches the same requests, and several origins are all logged', () => {
  const { session, log } = setup([`${ORIGIN}/`, 'http://127.0.0.1:5001']);
  session.start('a', `${ORIGIN}/zarr.json`, 1);
  session.start('b', 'http://127.0.0.1:5001/demo/index.html', 1, 'Document');
  assert.equal(log.snapshot().requests, 2);
  assert.equal(log.entries()[1].type, 'Document');
});

test('events of a request that was never logged are ignored', () => {
  const { session, log } = setup();
  session.data('unknown', 100);
  session.finish('unknown', 5, 100);
  session.fail('unknown', 5);
  assert.deepEqual(log.snapshot(), { requests: 0, bytes: 0, failed: 0 });
});

test('the traffic of a scenario is the difference of two snapshots; a request in flight at the start adds its later bytes but is not counted', () => {
  const { session, log } = setup();
  session.start('early', `${ORIGIN}/0/data/c/0/0/0/0`, 10);
  session.data('early', 400_000);
  session.finish('early', 10.5, 400_100);
  session.start('inflight', `${ORIGIN}/1/data/c/0/0/0/0`, 10.6);
  session.data('inflight', 100_000);
  const before = log.snapshot();
  session.data('inflight', 300_000);
  session.finish('inflight', 11, 400_200);
  session.start('scenario', `${ORIGIN}/2/data/c/5/0/0/0`, 11.2);
  session.data('scenario', 50_000);
  session.fail('scenario', 11.3);
  const after = log.snapshot();
  assert.deepEqual(after, { requests: 3, bytes: 400_100 + 400_200 + 50_000, failed: 1 });
  // The request that started before the scenario costs the scenario the rest of its bytes (300,200: its total less the 100,000 already counted) and no request.
  assert.deepEqual({ requests: after.requests - before.requests, bytes: after.bytes - before.bytes, failed: after.failed - before.failed }, { requests: 1, bytes: 300_200 + 50_000, failed: 1 });
});

test('attaching turns the network events on and the HTTP cache off, unless asked to leave it on', async () => {
  const session = new FakeSession();
  const context = { newCDPSession: async () => session };
  const { log } = await attachNetworkLog(context, {}, [ORIGIN]);
  assert.ok(log instanceof NetworkLog);
  assert.deepEqual(session.sent, [['Network.enable', undefined], ['Network.setCacheDisabled', { cacheDisabled: true }]]);
  const kept = new FakeSession();
  await attachNetworkLog({ newCDPSession: async () => kept }, {}, [ORIGIN], { cacheDisabled: false });
  assert.deepEqual(kept.sent[1], ['Network.setCacheDisabled', { cacheDisabled: false }]);
});

test('a link is the download rate in bytes per second and the delay of CDP; null takes the throttling off', async () => {
  const session = new FakeSession();
  await setLink(session, { mbps: 8, latencyMs: 80 });
  await setLink(session, null);
  assert.deepEqual(session.sent, [
    ['Network.emulateNetworkConditions', { offline: false, latency: 80, downloadThroughput: 1_000_000, uploadThroughput: 1_000_000 }],
    ['Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }],
  ]);
});
