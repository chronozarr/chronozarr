// Minimal static file server with GET, HEAD and byte ranges (single range, including suffix; a range that starts
// beyond the end of the file answers 416 like real hosts do). Used to test the decoder over real HTTP against the
// fixture stores.

import http from 'node:http';
import { createReadStream } from 'node:fs';
import { stat } from 'node:fs/promises';
import path from 'node:path';

const CONTENT_TYPES = {
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.html': 'text/html',
  '.css': 'text/css',
  '.json': 'application/json',
  '.wasm': 'application/wasm',
};

export async function startStaticServer(rootDir, port = 0) {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const file = path.join(rootDir, decodeURIComponent(url.pathname));
    requests.push({ method: req.method, path: url.pathname, range: req.headers.range ?? null, cacheControl: req.headers['cache-control'] ?? null });
    try {
      const info = await stat(file);
      if (!info.isFile()) throw new Error('not a file');
      const size = info.size;
      const match = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? '');
      let start = 0;
      let end = size - 1;
      let status = 200;
      if (match) {
        status = 206;
        if (match[1] === '') start = Math.max(0, size - Number(match[2]));
        else {
          start = Number(match[1]);
          if (match[2] !== '') end = Math.min(size - 1, Number(match[2]));
        }
        if (start >= size) {
          res.writeHead(416, { 'Content-Range': `bytes */${size}`, 'Access-Control-Allow-Origin': '*' });
          return res.end();
        }
      }
      const headers = {
        'Content-Length': end - start + 1,
        'Content-Type': CONTENT_TYPES[path.extname(file)] ?? 'application/octet-stream',
        'Access-Control-Allow-Origin': '*',
        'Accept-Ranges': 'bytes',
      };
      if (status === 206) headers['Content-Range'] = `bytes ${start}-${end}/${size}`;
      res.writeHead(status, headers);
      if (req.method === 'HEAD') return res.end();
      createReadStream(file, { start, end }).pipe(res);
    } catch {
      res.writeHead(404);
      res.end();
    }
  });
  await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
  const { port: boundPort } = server.address();
  return {
    url: `http://127.0.0.1:${boundPort}`,
    requests,
    close: () => new Promise((resolve) => server.close(resolve)),
  };
}
