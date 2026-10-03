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
    return env.ASSETS.fetch(request);
  },
};
