// Paths of pages that moved or were removed. Each old path goes to the page that now holds its content.
// Old section anchors are mapped in the browser (legacy-anchors.json), because a fragment never reaches the server.
import moved from './redirects.json';

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
    return env.ASSETS.fetch(request);
  },
};
