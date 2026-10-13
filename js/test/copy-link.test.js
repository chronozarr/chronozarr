import { test } from 'node:test';
import assert from 'node:assert/strict';
import { bindCopyLink, copyLink, linksToThisComputer } from '../demo/copy-link.js';

function fakeElement(textContent = '') {
  const listeners = new Map();
  return {
    textContent,
    addEventListener(name, callback) {
      listeners.set(name, callback);
    },
    async click() {
      await listeners.get('click')?.();
    },
  };
}

function fakeTimer() {
  let callback = null;
  return {
    setTimeout(next) {
      callback = next;
      return 1;
    },
    clearTimeout() {
      callback = null;
    },
    fire() {
      callback?.();
    },
  };
}

test('copies the supplied redacted permalink and briefly announces success', async () => {
  const button = fakeElement('Copy link');
  const status = fakeElement();
  const timer = fakeTimer();
  const copied = [];
  const { copy } = bindCopyLink({
    button,
    status,
    getUrl: () => 'https://chronozarr.org/demo/?store=https%3A%2F%2Fdata.example%2Fstore&t=2',
    clipboard: { writeText: async (url) => copied.push(url) },
    timer,
  });

  assert.equal(await copy(), true);
  assert.deepEqual(copied, ['https://chronozarr.org/demo/?store=https%3A%2F%2Fdata.example%2Fstore&t=2']);
  assert.equal(button.textContent, 'Link copied');
  assert.equal(status.textContent, 'Link copied.');
  timer.fire();
  assert.equal(button.textContent, 'Copy link');
  assert.equal(status.textContent, '');
});

test('does not report success when clipboard access is unavailable', async () => {
  const button = fakeElement('Copy link');
  const status = fakeElement();
  const { copy } = bindCopyLink({ button, status, getUrl: () => 'https://chronozarr.org/demo/', clipboard: undefined, timer: fakeTimer() });

  assert.equal(await copy(), false);
  assert.equal(button.textContent, 'Copy address bar');
  assert.equal(status.textContent, 'Could not copy the link. Copy the address bar.');
  await assert.rejects(copyLink('https://chronozarr.org/demo/', { clipboard: undefined }), /Clipboard access is unavailable/);
});

test('does nothing until a viewer has a current link', async () => {
  const button = fakeElement('Copy link');
  const status = fakeElement();
  const { copy } = bindCopyLink({ button, status, getUrl: () => null, clipboard: { writeText: async () => assert.fail('must not copy') }, timer: fakeTimer() });
  assert.equal(await copy(), false);
  assert.equal(button.textContent, 'Copy link');
  assert.equal(status.textContent, '');
});

test('a link to a store on this computer says that it opens nowhere else', async () => {
  const button = fakeElement('Copy link');
  const status = fakeElement();
  const { copy } = bindCopyLink({
    button,
    status,
    getUrl: () => 'https://chronozarr.org/demo/?store=http%3A%2F%2F127.0.0.1%3A8765%2Fmy_store&t=2',
    clipboard: { writeText: async () => {} },
    timer: fakeTimer(),
  });
  assert.equal(await copy(), true);
  assert.equal(button.textContent, 'Copied (local only)');
  assert.match(status.textContent, /only on this computer/);
  assert.match(status.textContent, /chronozarr share/);
});

test('links to a store on this computer are told from shared and published ones', () => {
  const at = (store) => `https://chronozarr.org/demo/?store=${encodeURIComponent(store)}`;
  assert.equal(linksToThisComputer(at('http://127.0.0.1:8765/s')), true);
  assert.equal(linksToThisComputer(at('http://localhost:8000/s')), true);
  assert.equal(linksToThisComputer('http://localhost:8000/demo/?store=/data/s'), true, 'a self-hosted viewer on this computer');
  assert.equal(linksToThisComputer(at('https://bright-sea.trycloudflare.com/s')), false);
  assert.equal(linksToThisComputer(at('https://data.chronozarr.org/ucayali_santa_maria_v03')), false);
  assert.equal(linksToThisComputer('not a url'), false);
});
