// Paths of pages that moved or were removed. Each old path goes to the page that now holds its content.
// Old section anchors are mapped in the browser (legacy-anchors.json), because a fragment never reaches the server.
import moved from './redirects.json' with { type: 'json' };

// The viewer page carries no analytics. `no-transform` tells Cloudflare not to rewrite the response, so it does not
// inject its Web Analytics beacon here (nor compress this one page). The docs pages keep the zone's settings.
const VIEWER_PAGES = new Set(['/demo/', '/demo/index.html']);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.hostname === 'www.chronozarr.org') {
      url.hostname = 'chronozarr.org';
      return Response.redirect(url.toString(), 301);
    }
    if (url.pathname === '/demo') {
      url.pathname = '/demo/';
      return Response.redirect(url.toString(), 301);
    }
    const target = moved[url.pathname.replace(/\/$/, '')];
    if (target) return Response.redirect(new URL(target, url).toString(), 301);
    const response = await env.ASSETS.fetch(request);
    if (!VIEWER_PAGES.has(url.pathname) || !(response.headers.get('content-type') ?? '').startsWith('text/html')) {
      return response;
    }
    const headers = new Headers(response.headers);
    headers.set('Cache-Control', `${headers.get('Cache-Control') ?? 'public, max-age=0, must-revalidate'}, no-transform`);
    return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
  },
};
