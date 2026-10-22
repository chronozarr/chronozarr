// A local stand-in for chronozarr.org and data.chronozarr.org, to measure the viewer's cold open without the network.
//
//   node scripts/fieldperf/local_site.mjs --store <dir>:<port> --site <name>=<dir>:<port> [--site ...]
//
// Every mount is its own origin (https://127.0.0.1:<port>), as the site and the data host are, and speaks HTTP/2 with byte
// ranges and CORS, so the requests share one connection per origin as they do on Cloudflare. The certificate is self-signed
// (made with openssl at start): run the measurement scripts with FIELDPERF_INSECURE=1. Prints one JSON line when it listens.
//
// `--store` serves a directory of stores (the store at <dir>/<name> is https://127.0.0.1:<port>/<name>); each `--site` serves
// a copy of the site (viewer at /demo/index.html). Stops on SIGINT or SIGTERM.

import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { startStaticServer } from '../../js/support/static-server.js';

const args = process.argv.slice(2);
const mounts = [];
for (let i = 0; i < args.length; i += 2) {
  const flag = args[i];
  const value = args[i + 1];
  const match = /^(?:(?<name>[\w-]+)=)?(?<dir>.+):(?<port>\d+)$/.exec(value ?? '');
  if ((flag !== '--store' && flag !== '--site') || !match || (flag === '--site' && !match.groups.name)) {
    console.error('Usage: local_site.mjs --store <dir>:<port> --site <name>=<dir>:<port> [--site ...]');
    process.exit(1);
  }
  mounts.push({ name: flag === '--store' ? 'store' : match.groups.name, dir: path.resolve(match.groups.dir), port: Number(match.groups.port) });
}

const certDir = mkdtempSync(path.join(os.tmpdir(), 'fieldperf-cert-'));
execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1', '-keyout', path.join(certDir, 'key.pem'), '-out', path.join(certDir, 'cert.pem')], { stdio: 'ignore' });
const tls = { key: readFileSync(path.join(certDir, 'key.pem')), cert: readFileSync(path.join(certDir, 'cert.pem')) };
rmSync(certDir, { recursive: true, force: true });

const servers = await Promise.all(mounts.map(({ dir, port }) => startStaticServer(dir, port, { tls })));
console.log(JSON.stringify(Object.fromEntries(mounts.map(({ name }, i) => [name, servers[i].url]))));
// An HTTP/2 server's close() waits for the browsers' open sessions; exiting is what stops it.
process.on('SIGINT', () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));
