// A local copy of chronozarr.org over HTTPS: the built site (site/docs/dist) behind the same rules as the Worker
// (site/worker.js) and the host's static assets, so that a container whose chronozarr.org points here meets a release
// that has not been deployed.
//
//   node scripts/trial/newcomer/site_server.mjs --dir site/docs/dist --redirects site/redirects.json \
//     --cert site.pem --key site.key [--port 443] [--log requests.jsonl]
//
// Behaviour copied from the live site (checked with curl on 2026-10-20): /demo goes 301 to /demo/; the paths in
// redirects.json go 301 to their new page; a directory with an index.html answers /name with 307 to /name/; the
// /demo/ page adds no-transform to Cache-Control; the rules of dist/_headers apply (a later rule replaces an earlier
// one's header of the same name); a missing path answers 404 with 404.html. Speaks HTTP/2 and HTTP/1.1. Listens on
// every interface of the host (port 443 needs no privilege on macOS). Logs one JSON line per request to --log.

import http2 from 'node:http2';
import { appendFileSync, createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';

const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) args.set(process.argv[i], process.argv[i + 1]);
const need = (flag) => {
  if (!args.get(flag)) {
    console.error('Usage: site_server.mjs --dir DIST --redirects redirects.json --cert PEM --key PEM [--port N] [--log FILE]');
    process.exit(1);
  }
  return args.get(flag);
};
const root = path.resolve(need('--dir'));
const moved = JSON.parse(readFileSync(need('--redirects'), 'utf8'));
const tls = { key: readFileSync(need('--key')), cert: readFileSync(need('--cert')), allowHTTP1: true };
const port = Number(args.get('--port') ?? 443);
const logFile = args.get('--log');

const CONTENT_TYPES = {
  '.html': 'text/html',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.xml': 'application/xml',
  '.txt': 'text/plain; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.wasm': 'application/wasm',
  '.woff2': 'font/woff2',
  '.md': 'text/markdown; charset=utf-8',
};

// dist/_headers: a pattern line (no indent) followed by indented `Name: value` lines. `*` matches anything.
function parseHeaders(file) {
  if (!existsSync(file)) return [];
  const rules = [];
  for (const line of readFileSync(file, 'utf8').split('\n')) {
    if (line.trim() === '' || line.trim().startsWith('#')) continue;
    if (!/^\s/.test(line)) {
      const source = line.trim().replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
      rules.push({ pattern: new RegExp(`^${source}$`), headers: [] });
    } else {
      const at = line.indexOf(':');
      rules[rules.length - 1].headers.push([line.slice(0, at).trim().toLowerCase(), line.slice(at + 1).trim()]);
    }
  }
  return rules;
}
const headerRules = parseHeaders(path.join(root, '_headers'));

function rulesFor(pathname) {
  const headers = {};
  for (const rule of headerRules) {
    if (rule.pattern.test(pathname)) for (const [name, value] of rule.headers) headers[name] = value;
  }
  return headers;
}

function resolve(pathname) {
  const decoded = decodeURIComponent(pathname);
  const target = path.join(root, decoded);
  if (!target.startsWith(root)) return { kind: 'missing' };
  if (existsSync(target) && statSync(target).isFile()) return { kind: 'file', file: target };
  if (existsSync(target) && statSync(target).isDirectory() && existsSync(path.join(target, 'index.html'))) {
    return decoded.endsWith('/') ? { kind: 'file', file: path.join(target, 'index.html') } : { kind: 'slash' };
  }
  return { kind: 'missing' };
}

function send(res, status, headers, file, method) {
  const size = file ? statSync(file).size : 0;
  res.writeHead(status, file ? { ...headers, 'content-length': size } : headers);
  if (!file || method === 'HEAD') return res.end();
  createReadStream(file).pipe(res);
}

const handler = (req, res) => {
  const authority = req.headers[':authority'] ?? req.headers.host ?? 'chronozarr.org';
  const url = new URL(req.url, `https://${authority}`);
  const pathname = url.pathname;
  let status;
  const done = (code, headers, file) => {
    status = code;
    send(res, code, headers, file, req.method);
    if (logFile) {
      appendFileSync(
        logFile,
        `${JSON.stringify({ t: new Date().toISOString(), method: req.method, path: pathname + url.search, status, ua: (req.headers['user-agent'] ?? '').slice(0, 60), from: req.socket.remoteAddress })}\n`,
      );
    }
  };
  const redirect = (code, location) => done(code, { location });

  if (pathname === '/demo') return redirect(301, `https://${authority}/demo/`);
  const target = moved[pathname.replace(/\/$/, '')];
  if (target) return redirect(301, new URL(target, url).toString());

  const found = resolve(pathname);
  if (found.kind === 'slash') return redirect(307, `${pathname}/${url.search}`);
  const base = { 'cache-control': 'public, max-age=0, must-revalidate', ...rulesFor(pathname) };
  if (found.kind === 'missing') {
    const page = path.join(root, '404.html');
    return done(404, { ...base, 'content-type': 'text/html' }, existsSync(page) ? page : null);
  }
  const headers = { ...base };
  headers['content-type'] ??= CONTENT_TYPES[path.extname(found.file)] ?? 'application/octet-stream';
  if (pathname === '/demo/' || pathname === '/demo/index.html') headers['cache-control'] = `${headers['cache-control']}, no-transform`;
  done(200, headers, found.file);
};

const server = http2.createSecureServer(tls, handler);
server.listen(port, () => {
  console.log(JSON.stringify({ listening: port, dir: root, log: logFile ?? null }));
});
// An HTTP/2 server's close() waits for open sessions; exiting is what stops it.
process.on('SIGINT', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));
