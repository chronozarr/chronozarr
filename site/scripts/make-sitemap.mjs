// Writes docs/dist/sitemap.xml from the pages the build produced, so search engines find every page.
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const site = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const dist = resolve(site, 'docs/dist');
// Archived records are served but not listed (archived-pages.json).
const archived = JSON.parse(readFileSync(resolve(site, 'archived-pages.json'), 'utf8')).routes;

function pages(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return pages(path);
    // Docs pages only: the copied viewer and MapLibre demo pages are not Vocs pages.
    if (name !== 'index.html' || !readFileSync(path, 'utf8').includes('id="vocs-content"')) return [];
    return [`/${dirname(relative(dist, path))}`.replace(/^\/\.$/, '/')];
  });
}

const routes = pages(dist).filter((route) => !archived.includes(route)).sort();
const urls = routes.map((route) => `  <url><loc>https://chronozarr.org${route}</loc></url>`).join('\n');
writeFileSync(
  join(dist, 'sitemap.xml'),
  `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`,
);
console.log(`sitemap.xml: ${routes.length} pages.`);
