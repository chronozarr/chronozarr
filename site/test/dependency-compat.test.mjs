import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import test from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import * as jsxRuntime from 'react/jsx-runtime';

// Exercise the exact parser/renderer dependencies used by the pinned framework.
const require = createRequire(import.meta.resolve('vocs'));
const load = name => import(pathToFileURL(require.resolve(name)).href);

test('patched TOML parser preserves Vocs static configuration', async () => {
  const { default: toml, parse } = await load('toml');
  assert.equal(typeof parse, 'function'); // remark-mdx-frontmatter's named import
  assert.equal(typeof toml.parse, 'function'); // Vocs' default import
  const dir = await mkdtemp(join(tmpdir(), 'chronozarr-vocs-toml-'));
  const previous = process.cwd();
  try {
    await writeFile(join(dir, 'vocs.config.toml'), `title = "Parser fixture"
description = "TOML configuration remains supported"
base_url = "/docs"
[theme]
accent_color = "blue"
`);
    const configModule = resolve(dirname(require.resolve('vocs')), 'vite/utils/resolveVocsConfig.js');
    const { resolveVocsConfig } = await import(pathToFileURL(configModule).href);
    process.chdir(dir);
    const { config, configPath } = await resolveVocsConfig();
    assert.equal(configPath, 'vocs.config.toml');
    assert.equal(config.title, 'Parser fixture');
    assert.equal(config.description, 'TOML configuration remains supported');
    assert.equal(config.baseUrl, '/docs');
    assert.equal(config.theme.accentColor.textAccent.light, 'blue');
    assert.throws(() => parse('title = ["unterminated"'), SyntaxError);
  } finally {
    process.chdir(previous);
    await rm(dir, { recursive: true, force: true });
  }
});

test('YAML and TOML frontmatter still compile with Markdown and MDX', async () => {
  const [{ evaluate }, { default: frontmatter }, { default: mdxFrontmatter }, { default: gfm }] =
    await Promise.all(['@mdx-js/mdx', 'remark-frontmatter', 'remark-mdx-frontmatter', 'remark-gfm'].map(load));
  const fixtures = [
    '---\ntitle: Metadata fixture\nsteps: [1, 2]\nscale: 0.1\n---',
    '+++\ntitle = "Metadata fixture"\nsteps = [1, 2]\nscale = 0.1\n+++',
  ];
  for (const header of fixtures) {
    const module = await evaluate(`${header}

# Markdown fixture

Stored **values** and \`uint16\` stay intact.

| Band | Value |
| --- | --- |
| red | 42 |

<span data-fixture="mdx">MDX works</span>
`, {
      ...jsxRuntime,
      remarkPlugins: [[frontmatter, ['yaml', 'toml']], mdxFrontmatter, gfm],
    });
    assert.equal(module.frontmatter.title, 'Metadata fixture');
    assert.deepEqual(module.frontmatter.steps, [1, 2]);
    assert.equal(module.frontmatter.scale, 0.1);
    assert.equal(typeof module.frontmatter.steps[0], 'number');
    const html = renderToStaticMarkup(jsxRuntime.jsx(module.default, {}));
    assert.match(html, /<h1>Markdown fixture<\/h1>/);
    assert.match(html, /<strong>values<\/strong>/);
    assert.match(html, /<code>uint16<\/code>/);
    assert.match(html, /<table>/);
    assert.match(html, /<td>42<\/td>/);
    assert.match(html, /data-fixture="mdx">MDX works/);
  }
});

test('patched KaTeX keeps public math rendering and safe URL behavior', async () => {
  const { default: katex } = await load('katex');
  assert.equal(katex.version, '0.18.2');
  const html = katex.renderToString(String.raw`\frac{x^2}{2}`, { throwOnError: true, output: 'mathml' });
  assert.match(html, /<mfrac>/);
  assert.match(html, /<msup>/);
  const unsafe = katex.renderToString(String.raw`\href{javascript:alert(1)}{link}`, { trust: false, strict: 'ignore' });
  assert.doesNotMatch(unsafe, /href="javascript:/);
});

test('rebuilt Mermaid renders math and sanitizes HTML through the real Vocs renderer', async () => {
  const { createMermaidRenderer } = await load('mermaid-isomorphic');
  const render = createMermaidRenderer();
  const [result] = await render([String.raw`flowchart LR
  A["$$\frac{x^2}{2}$$"] --> B["<span onmouseover='alert(1)'>Clean label</span>"]
`], { mermaidConfig: { securityLevel: 'strict' } });
  assert.equal(result.status, 'fulfilled', result.reason?.stack);
  assert.ok(result.value.width > 0 && result.value.height > 0);
  assert.match(result.value.svg, /<mfrac>/);
  assert.match(result.value.svg, /<msup>/);
  assert.match(result.value.svg, /Clean label/);
  assert.doesNotMatch(result.value.svg, /onmouseover\s*=/);
  assert.doesNotMatch(result.value.svg, /<script\b/);
});

test('prepared classic bundle is reproducible and contains the patched modules', async () => {
  const core = require.resolve('mermaid');
  const bundle = await readFile(resolve(dirname(core), 'mermaid.js'), 'utf8');
  assert.match(bundle, /DOMPurify\.version = "3\.4\.17"/);
  assert.match(bundle, /version\w* = "0\.18\.2"/);
  assert.doesNotMatch(bundle, /DOMPurify\.version = "3\.4\.(?:12|16)"/);
  assert.doesNotMatch(bundle, /version\w* = "0\.16\.47"/);
  assert.match(bundle, /globalThis\.mermaid = /);
  const script = new URL('../scripts/prepare-mermaid.mjs', import.meta.url);
  await promisify(execFile)(process.execPath, [fileURLToPath(script)]);
  assert.equal(await readFile(resolve(dirname(core), 'mermaid.js'), 'utf8'), bundle);
});

test('Vocs Mermaid plugin produces light and dark SVG images from a math fence', async () => {
  const [{ evaluate }, { default: mermaid }] = await Promise.all(['@mdx-js/mdx', 'rehype-mermaid'].map(load));
  const module = await evaluate('```mermaid\nflowchart LR\nA["$$x^2$$"] --> B["Stored values"]\n```', {
    ...jsxRuntime,
    rehypePlugins: [[mermaid, { dark: true, strategy: 'img-svg' }]],
  });
  const html = renderToStaticMarkup(jsxRuntime.jsx(module.default, {}));
  const images = [...html.matchAll(/data:image\/svg\+xml,([^"]+)/g)];
  assert.ok(images.length >= 2, 'light and dark prerendered images');
  for (const [, encoded] of images) {
    const svg = decodeURIComponent(encoded);
    assert.match(svg, /<svg\b/);
    assert.match(svg, /<msup>/);
    assert.match(svg, /Stored values/);
  }
});
