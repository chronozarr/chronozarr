// Fails the build when a link on a built page, a path redirect (redirects.json), an old section anchor
// (legacy-anchors.json) or an address that chronozarr.org has published (published-anchors.json) leads to a page or a
// section that the build does not contain.
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const site = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const dist = resolve(site, 'docs/dist');
const redirects = JSON.parse(readFileSync(resolve(site, 'redirects.json'), 'utf8'));
const legacy = JSON.parse(readFileSync(resolve(site, 'legacy-anchors.json'), 'utf8'));
const published = JSON.parse(readFileSync(resolve(site, 'published-anchors.json'), 'utf8')).pages;
const origin = 'https://chronozarr.org';

function htmlFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return htmlFiles(path);
    return name.endsWith('.html') ? [path] : [];
  });
}

// Vocs writes each page as <route>/index.html. The copied viewer (demo/) is a static page, not a docs route.
const pages = new Map();
for (const file of htmlFiles(dist)) {
  const path = relative(dist, file);
  if (!path.endsWith('index.html') || path.startsWith('demo/')) continue;
  const route = `/${dirname(path)}`.replace(/^\/\.$/, '/');
  const html = readFileSync(file, 'utf8');
  pages.set(route, { html, ids: new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1])) });
}

const isStaticFile = (path) => {
  const file = join(dist, decodeURIComponent(path));
  return existsSync(file) && (statSync(file).isFile() || existsSync(join(file, 'index.html')));
};

/** Where an internal href ends up, after path redirects and old-anchor mapping. Returns an error or null. */
function check(href, depth = 0) {
  if (depth > 3) return 'redirect loop';
  const url = new URL(href, `${origin}/`);
  if (url.origin !== origin) return null;
  const path = url.pathname.length > 1 ? url.pathname.replace(/\/$/, '') : '/';
  const anchor = decodeURIComponent(url.hash.slice(1));
  if (redirects[path]) return check(redirects[path] + url.hash, depth + 1);
  const page = pages.get(path);
  if (!page) return isStaticFile(url.pathname) ? null : `no page ${path}`;
  if (!anchor) return null;
  const moved = legacy[path]?.[anchor];
  if (moved) return check(moved, depth + 1);
  return page.ids.has(anchor) ? null : `no section #${anchor} on ${path}`;
}

const problems = [];
for (const [route, { html }] of pages) {
  for (const [, raw] of html.matchAll(/\shref="([^"]+)"/g)) {
    const href = raw.replaceAll('&amp;', '&');
    if (/^(?:mailto:|javascript:|data:)/.test(href)) continue;
    if (/^https?:\/\//.test(href) && !href.startsWith(origin)) continue;
    const problem = check(href.startsWith('/') || href.startsWith('http') ? href : new URL(href, `${origin}${route}/`).href);
    if (problem) problems.push(`${route}: ${href}: ${problem}`);
  }
}
for (const [from, to] of Object.entries(redirects)) {
  if (pages.has(from)) problems.push(`redirects.json: ${from} is also a page, so the redirect hides it`);
  const problem = check(to);
  if (problem) problems.push(`redirects.json: ${from} -> ${to}: ${problem}`);
}
for (const [route, anchors] of Object.entries(legacy)) {
  if (!pages.has(route)) problems.push(`legacy-anchors.json: no page ${route}`);
  for (const [anchor, to] of Object.entries(anchors)) {
    if (pages.get(route)?.ids.has(anchor)) {
      problems.push(`legacy-anchors.json: ${route}#${anchor} is a live section, so the mapping would leave it`);
    }
    const problem = check(to);
    if (problem) problems.push(`legacy-anchors.json: ${route}#${anchor} -> ${to}: ${problem}`);
  }
}

for (const [route, anchors] of Object.entries(published)) {
  for (const href of [route, ...anchors.map((anchor) => `${route}#${anchor}`)]) {
    const problem = check(href);
    if (problem) problems.push(`published-anchors.json: ${href}: ${problem}`);
  }
}

if (problems.length) {
  console.error(`${problems.length} broken link(s):\n${problems.map((p) => `  ${p}`).join('\n')}`);
  process.exit(1);
}
console.log(`Links: ${pages.size} pages; every internal link, redirect, old anchor and published address resolves.`);
