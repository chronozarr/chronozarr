// The viewer's layout contract: the breakpoint helper, and the stylesheet and markup of index.html that implement it.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { COMPACT_BELOW, DRAWER_BELOW, inspectorLayout } from '../demo/layout.js';

const html = readFileSync(new URL('../demo/index.html', import.meta.url), 'utf8');
const css = html.slice(html.indexOf('<style>'), html.indexOf('</style>'));

test('the inspector is a side panel from 900 px and a drawer below; the boundary belongs to the panel', () => {
  assert.equal(DRAWER_BELOW, 900);
  assert.equal(inspectorLayout(360), 'drawer');
  assert.equal(inspectorLayout(803), 'drawer');
  assert.equal(inspectorLayout(899), 'drawer');
  assert.equal(inspectorLayout(899.5), 'drawer');
  assert.equal(inspectorLayout(900), 'panel');
  assert.equal(inspectorLayout(1440), 'panel');
});

test('index.html uses exactly the breakpoints of layout.js in its media queries', () => {
  const widths = [...css.matchAll(/@media \(width < (\d+)px\)/g)].map((m) => Number(m[1]));
  assert.deepEqual([...new Set(widths)].sort((a, b) => b - a), [DRAWER_BELOW, COMPACT_BELOW]);
  assert.doesNotMatch(css, /@media \((max|min)-width/, 'no second spelling of a breakpoint that could drift from the helper');
});

test('the drawer rules live in the DRAWER_BELOW query and the compact product select in the COMPACT_BELOW one', () => {
  const block = (below) => {
    const start = css.indexOf(`@media (width < ${below}px)`);
    assert.ok(start >= 0, `a ${below}px query exists`);
    let depth = 0;
    for (let i = css.indexOf('{', start); i < css.length; i++) {
      if (css[i] === '{') depth++;
      if (css[i] === '}' && --depth === 0) return css.slice(start, i + 1);
    }
    throw new Error('unbalanced CSS');
  };
  const drawer = block(DRAWER_BELOW);
  assert.match(drawer, /\.sidebar\s*\{[^}]*position: absolute/);
  assert.match(drawer, /\.sidebar\.open\s*\{[^}]*visibility: visible/);
  const compact = block(COMPACT_BELOW);
  assert.match(compact, /\.products[^{]*\{\s*display: none/);
  assert.match(compact, /\.product-select\s*\{\s*display: block/);
  assert.match(css, /\.product-select \{ display: none; \}/, 'buttons are the default; the select only appears in the compact query');
});

test('the header holds navigation and product controls only: every diagnostic figure lives in the d overlay', () => {
  const nav = html.slice(html.indexOf('<nav>'), html.indexOf('</nav>'));
  for (const id of ['cache-stats', 'nav-meta']) assert.doesNotMatch(html, new RegExp(`id="${id}"`), id);
  assert.doesNotMatch(html, /id="status"/, 'the paint time is an overlay row, not a figure on the timeline bar');
  assert.match(nav, /id="products"/);
  assert.match(nav, /id="product-select"/);
  assert.match(html, /id="perf-overlay"/);
  assert.match(html, /id="inspector-close"/, 'a drawer needs a way to close on a touch screen');
});

test('the inspector drawer cannot be reached by keyboard or screen reader while closed', () => {
  assert.match(css, /\.sidebar \{[^}]*visibility: hidden;/, 'a closed drawer is visibility: hidden');
});
