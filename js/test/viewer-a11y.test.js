// The viewer's accessibility contract in index.html: palette contrast (WCAG AA), 44 px targets for a finger, names for controls.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../demo/index.html', import.meta.url), 'utf8');
const css = html.slice(html.indexOf('<style>'), html.indexOf('</style>'));

/** The custom properties declared by the first rule that starts with `selector`. */
function palette(selector) {
  const start = css.indexOf(`${selector} {`);
  assert.ok(start >= 0, `a rule for ${selector}`);
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start));
  return Object.fromEntries([...body.matchAll(/--([a-z0-9-]+):\s*(#[0-9a-f]{6})\b/gi)].map((m) => [m[1], m[2]]));
}

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

for (const [name, selector] of [['dark', ':root'], ['light', ':root[data-theme="light"]']]) {
  test(`${name} palette: text, secondary text and hints reach 4.5:1 on every surface; selected controls hold white text`, () => {
    const colors = palette(selector);
    for (const text of ['text', 'text-2', 'text-3']) {
      for (const surface of ['bg', 'surface', 'surface-2']) {
        assert.ok(contrast(colors[text], colors[surface]) >= 4.5, `--${text} on --${surface}: ${contrast(colors[text], colors[surface]).toFixed(2)}`);
      }
    }
    assert.ok(contrast('#ffffff', colors['accent-solid']) >= 4.5, `white on --accent-solid: ${contrast('#ffffff', colors['accent-solid']).toFixed(2)}`);
    assert.ok(contrast(colors.accent, colors.surface) >= 4.5, `--accent text on --surface: ${contrast(colors.accent, colors.surface).toFixed(2)}`);
  });
}

test('selected and primary controls fill with --accent-solid, not the lighter --accent that fails under white text', () => {
  assert.match(css, /\.products button\.active \{[^}]*background: var\(--accent-solid\)/);
  assert.match(css, /\.play-btn\.playing \{[^}]*background: var\(--accent-solid\)/);
  assert.match(css, /\.export-actions button, \.export-download \{[^}]*background: var\(--accent-solid\)/);
});

test('a coarse pointer gets targets of 44 px for the transport controls, the timeline, the selects and the product buttons', () => {
  const start = css.indexOf('@media (pointer: coarse) {');
  assert.ok(start >= 0, 'a coarse-pointer query exists');
  const block = css.slice(start, css.indexOf('\n  }\n', start));
  assert.match(block, /\.play-btn, \.month-nav button[^{]*\{ width: 44px; height: 44px; \}/);
  assert.match(block, /\.timeline-track \{ height: 44px; \}/);
  assert.match(block, /select, \.stretch input, \.stretch button, \.products button \{ min-height: 44px; \}/);
  assert.match(block, /select, \.stretch input \{ font-size: 16px; \}/, 'iOS Safari zooms the page when a field under 16 px takes focus');
  assert.ok(start > css.indexOf('/* ---- Embed (?embed=1)'), 'the query follows the embed rules, which would otherwise win at equal specificity');
});

test('every control has an accessible name and the timeline is a focusable slider', () => {
  assert.match(html, /<h1 class="brand">/);
  assert.match(html, /<select id="catalog-select" aria-label="Dataset"/);
  assert.match(html, /<select id="product-select"[^>]*aria-label="Product"/);
  assert.match(html, /<select id="band-select" aria-label="Band"/);
  assert.match(html, /<button id="prev-btn"[^>]*aria-label="Previous timestep"/);
  assert.match(html, /<button id="next-btn"[^>]*aria-label="Next timestep"/);
  assert.match(html, /<div class="timeline-track" id="timeline-track" role="slider" tabindex="0" aria-label="Timestep"/);
  assert.match(html, /<section class="timeline-bar" aria-label="Timeline">/);
  assert.match(html, /<div class="error-overlay" id="error-overlay" role="alert">/);
});

test('a focused control shows a ring, and no rule removes it', () => {
  assert.match(css, /:focus-visible \{\s*outline: 2px solid var\(--accent\);/);
  assert.doesNotMatch(css, /outline: none/);
});
