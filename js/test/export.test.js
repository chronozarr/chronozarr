import assert from 'node:assert/strict';
import { test } from 'node:test';
import { GIFEncoder, applyPalette, quantize } from '../vendor/gifenc.esm.js';
import { exportFilename, flipRows, gifDelayMs, paletteSampleTimes, pickWebmMimeType, subsamplePixels, webmBitrate } from '../demo/export.js';

test('gifDelayMs rounds to whole centiseconds and never goes below 20 ms', () => {
  assert.equal(gifDelayMs(1), 1000);
  assert.equal(gifDelayMs(4), 250);
  assert.equal(gifDelayMs(10), 100);
  assert.equal(gifDelayMs(30), 30);
  assert.equal(gifDelayMs(50), 20);
  assert.equal(gifDelayMs(60), 20);
});

test('pickWebmMimeType prefers VP9, then VP8, then plain WebM, and returns null when none is supported', () => {
  assert.equal(pickWebmMimeType(() => true), 'video/webm;codecs=vp9');
  assert.equal(pickWebmMimeType((type) => type !== 'video/webm;codecs=vp9'), 'video/webm;codecs=vp8');
  assert.equal(pickWebmMimeType((type) => type === 'video/webm'), 'video/webm');
  assert.equal(pickWebmMimeType(() => false), null);
});

test('webmBitrate scales with pixels and rate, within 2 and 24 Mbit/s', () => {
  assert.equal(webmBitrate(100, 100, 4), 2e6);
  assert.equal(webmBitrate(1600, 900, 10), 2160000);
  assert.equal(webmBitrate(4000, 3000, 60), 24e6);
  assert.ok(webmBitrate(1600, 900, 10) > webmBitrate(800, 450, 10));
});

test('paletteSampleTimes spreads distinct timesteps over the range, ends included', () => {
  assert.deepEqual(paletteSampleTimes(0, 116), [0, 23, 46, 70, 93, 116]);
  assert.deepEqual(paletteSampleTimes(5, 5), [5]);
  assert.deepEqual(paletteSampleTimes(3, 5), [3, 4, 5]);
  for (const t of paletteSampleTimes(10, 40)) assert.ok(t >= 10 && t <= 40);
});

test('flipRows reverses the row order and keeps the pixels in each row', () => {
  const rgba = Uint8Array.from([1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 6, 6, 6, 6]);
  assert.deepEqual([...flipRows(rgba, 2, 3)], [5, 5, 5, 5, 6, 6, 6, 6, 3, 3, 3, 3, 4, 4, 4, 4, 1, 1, 1, 1, 2, 2, 2, 2]);
  assert.deepEqual([...flipRows(flipRows(rgba, 2, 3), 2, 3)], [...rgba]);
});

test('subsamplePixels keeps every stride-th pixel and its whole RGBA quad', () => {
  const rgba = Uint8Array.from({ length: 7 * 4 }, (_, i) => Math.floor(i / 4));
  assert.deepEqual([...subsamplePixels(rgba, 3)], [0, 0, 0, 0, 3, 3, 3, 3, 6, 6, 6, 6]);
  assert.deepEqual([...subsamplePixels(rgba, 1)], [...rgba]);
});

test('exportFilename names the store, range and format, with only safe characters', () => {
  const labels = (t) => ['2015-01', '2015-02', '2024-09'][t];
  assert.equal(exportFilename('ucayali_santa_maria chronozarr-2', 'webm', 0, 2, labels), 'chronozarr-ucayali-santa-maria-chronozarr-2-2015-01-to-2024-09.webm');
  assert.equal(exportFilename('Stress 48', 'gif', 1, 1, labels), 'chronozarr-stress-48-2015-02-to-2015-02.gif');
});

test('the vendored GIF encoder writes a looping GIF89a with the frames, palette and delay we give it', () => {
  const width = 8;
  const height = 4;
  const frame = (r, g, b) => Uint8Array.from({ length: width * height * 4 }, (_, i) => [r, g, b, 255][i % 4]);
  const frames = [frame(255, 0, 0), frame(0, 255, 0), frame(0, 0, 255)];
  const sample = new Uint8Array(frames.flatMap((f) => [...f]));
  const palette = quantize(sample, 4);
  const gif = GIFEncoder();
  frames.forEach((rgba, i) => gif.writeFrame(applyPalette(rgba, palette), width, height, i === 0 ? { palette, delay: 100, repeat: 0 } : { delay: 100 }));
  gif.finish();
  const bytes = gif.bytes();
  assert.equal(String.fromCharCode(...bytes.slice(0, 6)), 'GIF89a');
  assert.equal(bytes[6] | (bytes[7] << 8), width);
  assert.equal(bytes[8] | (bytes[9] << 8), height);
  assert.equal(bytes[bytes.length - 1], 0x3b, 'trailer');
  const text = String.fromCharCode(...bytes);
  assert.ok(text.includes('NETSCAPE2.0'), 'loop extension');
  assert.equal(text.split('\x21\xf9\x04').length - 1, frames.length, 'one graphic control extension per frame');
  assert.ok(bytes.length < 400, `tiny flat frames compress to ${bytes.length} bytes`);
});
