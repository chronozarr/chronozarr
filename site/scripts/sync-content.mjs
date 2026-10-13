import { existsSync, readFileSync, readdirSync, writeFileSync, mkdirSync, rmSync, statSync } from 'node:fs';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
// Each repository document, the route it is published at, the description that search engines and link
// previews show, and a title where the document's own heading does not name the page.
const sources = {
  // Start here
  'docs/what-is.md': { route: 'what-is-chronozarr', description: 'What chronozarr does, how it compares with GIFs, folders of GeoTIFFs, plain Zarr and tile servers, and what it does not do.' },
  'docs/quickstart.md': { route: 'quickstart', description: 'Install chronozarr, convert dated GeoTIFFs, open the series in your browser and share a link, with a sample dataset.' },
  'docs/viewer.md': { route: 'viewer', description: 'The chronozarr viewer: playback, products, pixel time series, links to a view, and what the browser downloads.' },
  // Create and share
  'docs/convert.md': { route: 'guides/convert', description: 'Convert dated GeoTIFFs, COG manifests, NetCDF, Zarr or an xarray DataArray into a chronozarr store.' },
  'docs/preview.md': { route: 'guides/preview', description: 'Open a local chronozarr store in the viewer with chronozarr preview. The data stays on your computer.' },
  'docs/share.md': { route: 'guides/share', description: 'Send a temporary link to a store on your computer with chronozarr share: what the recipient needs, and the limits.' },
  'docs/publish.md': { route: 'guides/publish', description: 'Publish a store to Amazon S3, Cloudflare R2, Google Cloud Storage or Azure with chronozarr publish, for a link that lasts.' },
  'docs/analyze.md': { route: 'guides/analyze', description: 'Read a chronozarr store in Python with xarray: physical and stored values, pixel time series, indexes and export.' },
  // Guides and examples
  'docs/sentinel2.md': { route: 'guides/sentinel-2', description: 'Build a monthly Sentinel-2 composite time series for your own bounding box or GeoJSON area from Planetary Computer.' },
  'examples/bring_your_data/README.md': { route: 'guides/bring-your-data', description: 'A worked example: your own GeoTIFFs to a store, a pixel history, a static viewer folder and publishing.' },
  'docs/examples.md': { route: 'guides/examples', description: 'Worked examples for SWOT, NISAR, Sentinel-2, PNG frames and model output, and the conversion route for each.' },
  'docs/png-frames.md': { route: 'guides/png-frames', description: 'Convert georeferenced PNG frames, such as rendered map exports, into a chronozarr store.' },
  'docs/append.md': { route: 'guides/append', description: 'Add new timesteps to a chronozarr store, on your computer or after it is published.' },
  'docs/embedding.md': { route: 'guides/embedding', description: 'Embed the chronozarr viewer in your own web page with an iframe, URL parameters and messages.' },
  'docs/notebooks.md': { route: 'guides/notebooks', description: 'Show a chronozarr store in Jupyter or VS Code, on JupyterHub, over SSH and on other remote machines.' },
  'docs/private.md': { route: 'guides/private', description: 'Ways to view a chronozarr store that must not be public, and how to keep credentials out of links.' },
  'docs/hosting.md': { route: 'guides/hosting', description: 'Advanced hosting: manual host setup, hosts that chronozarr publish does not cover, and what a host must do.' },
  'docs/hosting-providers.md': { route: 'guides/hosting-providers', description: 'Host setup for chronozarr stores: Amazon S3, CloudFront, Cloudflare R2, Google Cloud Storage, Azure and Source Cooperative.' },
  'docs/hosting-requirements.md': { route: 'guides/hosting-requirements', description: 'What a static host must do to serve a chronozarr store, the chronozarr doctor checks, and how to verify by hand.' },
  'docs/viewer-distribution.md': { route: 'guides/viewer-distribution', description: 'Copy the chronozarr viewer from npm and host it next to your stores, with no request to chronozarr.org.' },
  // Reference
  'docs/cli.md': { route: 'reference/cli', description: 'Every chronozarr command: convert, preview, share, publish, doctor, link, append, export-cog and more.' },
  'docs/python.md': { route: 'reference/python', description: 'The chronozarr Python API: encode, open_store, the xarray backend, band roles and the notebook viewer.' },
  'js/README.md': { route: 'reference/javascript', title: 'JavaScript reader', description: 'The chronozarr npm package: a DOM-free reader, a MapLibre layer, a GeoLibre plugin and the packaged viewer.' },
  'js/maplibre/README.md': { route: 'reference/maplibre', description: 'Draw a chronozarr store on a MapLibre GL JS map with the chronozarr custom layer.' },
  'spec/CHRONOZARR.md': { route: 'specification', description: 'The chronozarr store format specification, v0.3 draft: Zarr v3 layout, metadata, pyramid, codecs and hosting rules.' },
  'docs/how-it-works.md': { route: 'how-it-works', description: 'How a chronozarr store is laid out, how the browser reads it, and how to read it without chronozarr.' },
  'docs/performance.md': { route: 'performance', description: 'Measured viewer speed, value correctness, Sentinel-2 ingest speed and memory, with methods, hardware and limits.' },
  'docs/evidence.md': { route: 'reference/evidence', title: 'Measurement log', description: 'Dated measurements, tested reader versions and observations behind the chronozarr documentation.' },
  'docs/troubleshooting.md': { route: 'troubleshooting', description: 'Fixes for common chronozarr problems: installation, conversion, the viewer, preview, share and publish.' },
  'docs/format-comparison.md': { route: 'guides/format-comparison', description: 'chronozarr compared with PMTiles, Mapbox raster-array, COG per date and CarbonPlan zarr-layer.' },
};

/** The page title: the override, or the first level-1 heading of the document. */
const titleOf = (source) =>
  sources[source].title ?? /^# (.+)$/m.exec(readFileSync(resolve(repo, source), 'utf8'))?.[1].replaceAll('`', '') ?? source;
const titles = Object.fromEntries(Object.keys(sources).map((source) => [source, titleOf(source)]));
// A link whose label is a file path (written for GitHub, as in [docs/append.md](docs/append.md)) reads as the title
// of the page it opens.
const PATH_LABEL = /^`?(?:\.\.\/)*(?:[\w.-]+\/)*[\w.-]+\.md(?:#[\w-]+)?`?$/;

// Every .md page is synced (site/.gitignore); index.mdx is the only hand-written page. Clear the synced
// pages first, so a page dropped from `sources` stops being published.
const pages = resolve(repo, 'site/docs/pages');
for (const directory of ['guides', 'reference']) {
  rmSync(resolve(pages, directory), { recursive: true, force: true });
}
for (const name of readdirSync(pages)) {
  if (name.endsWith('.md')) rmSync(resolve(pages, name));
}
for (const [source, { route, title, description }] of Object.entries(sources)) {
  let text = readFileSync(resolve(repo, source), 'utf8');
  text = text.replace(/(!?)\[([^\]]*)\]\(([^)]+)\)/g, (match, bang, label, href) => {
    // A GitHub link to a published document (npm renders js/README.md, so it links absolutely) opens the page.
    const github = /^https:\/\/github\.com\/chronozarr\/chronozarr\/blob\/main\/([^#]+)(#.*)?$/.exec(href);
    if (github && sources[github[1]]) href = `/${github[1]}${github[2] ?? ''}`;
    if (/^(?:https?:|#|mailto:)/.test(href)) return match;
    const [path, anchor] = href.split('#');
    const target = path.startsWith('/') ? path.slice(1) : relative(repo, resolve(repo, dirname(source), path));
    const isDirectory = existsSync(resolve(repo, target)) && statSync(resolve(repo, target)).isDirectory();
    const link = sources[target]
      ? `/${sources[target].route}`
      : `https://github.com/chronozarr/chronozarr/${isDirectory ? 'tree' : 'blob'}/main/${target}`;
    const shown = !bang && sources[target] && PATH_LABEL.test(label) ? titles[target] : label;
    return `${bang}[${shown}](${link}${anchor ? `#${anchor}` : ''})`;
  });
  if (title) text = text.replace(/^# .+$/m, `# ${title}`);
  // The README opens with repository status badges. A docs page does not show them.
  text = text.replace(/^\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)[ \t]*\n/gm, '');
  // Vocs parses Markdown as MDX: protect literal prose braces/angles, while
  // preserving fenced and inline code verbatim. No authored source is changed.
  text = text.replace(/<!--[\s\S]*?-->/g, '');
  text = text.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^`]*`+)/g)
    .map(part => part.startsWith('`') || part.startsWith('~~~') ? part
      : part.replaceAll('<', '&lt;').replaceAll('{', '&#123;').replaceAll('}', '&#125;'))
    .join('');
  // JSON strings are valid YAML scalars, so a title or description needs no further quoting.
  const frontmatter = `---\ntitle: ${JSON.stringify(titles[source])}\ndescription: ${JSON.stringify(description)}\n---\n\n`;
  const output = resolve(repo, `site/docs/pages/${route}.md`);
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, frontmatter + text);
}
console.log(`Synced ${Object.keys(sources).length} source documents.`);
