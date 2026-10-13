// Runs the production Worker (worker.js) against a stand-in for the static assets and fails the build when a moved
// path does not redirect where redirects.json says, or the viewer page could get Cloudflare's analytics injected.
import worker from '../worker.js';
import moved from '../redirects.json' with { type: 'json' };

const html = { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'public, max-age=0, must-revalidate' };
const env = {
  ASSETS: {
    fetch: async (request) => {
      const { pathname } = new URL(request.url);
      return new Response('<!doctype html><title>x</title>', { status: 200, headers: pathname.endsWith('.js') ? { 'content-type': 'text/javascript' } : html });
    },
  },
};
const get = (path) => worker.fetch(new Request(`https://chronozarr.org${path}`), env);
const problems = [];

for (const [from, to] of Object.entries(moved)) {
  for (const path of [from, `${from}/`]) {
    const response = await get(`${path}#kept`);
    const location = response.headers.get('location');
    if (response.status !== 301 || location !== `https://chronozarr.org${to}`) {
      problems.push(`${path}: ${response.status} ${location}, expected 301 to ${to}`);
    }
  }
}
for (const path of ['/demo/', '/demo/index.html']) {
  const cacheControl = (await get(path)).headers.get('cache-control') ?? '';
  if (!/\bno-transform\b/.test(cacheControl) || !/\bpublic\b/.test(cacheControl)) {
    problems.push(`${path}: Cache-Control "${cacheControl}" lacks public, no-transform, so Cloudflare would inject its beacon`);
  }
}
for (const path of ['/', '/quickstart', '/demo/viewer.js']) {
  const cacheControl = (await get(path)).headers.get('cache-control') ?? '';
  if (/no-transform/.test(cacheControl)) problems.push(`${path}: no-transform belongs to the viewer page only`);
}
const demo = await get('/demo');
if (demo.status !== 301 || !demo.headers.get('location')?.endsWith('/demo/')) problems.push('/demo does not redirect to /demo/');

if (problems.length) {
  console.error(`worker.js: ${problems.length} problem(s):\n${problems.map((p) => `  ${p}`).join('\n')}`);
  process.exit(1);
}
console.log(`worker.js: ${Object.keys(moved).length} redirects and the viewer page header behave.`);
