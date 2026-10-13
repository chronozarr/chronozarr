import { existsSync, readFileSync, writeFileSync, mkdirSync, rmSync, statSync } from 'node:fs';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const sources = {
  'README.md': 'getting-started',
  'examples/bring_your_data/README.md': 'guides/bring-your-data',
  'spec/CHRONOZARR.md': 'specification',
  'docs/hosting.md': 'guides/hosting',
  'docs/append.md': 'guides/append',
  'docs/embedding.md': 'guides/embedding',
  'docs/private.md': 'guides/private',
  'docs/viewer-distribution.md': 'guides/viewer-distribution',
  'docs/format-comparison.md': 'guides/format-comparison',
  'docs/png-frames.md': 'guides/png-frames',
  'docs/python.md': 'reference/python',
  'js/README.md': 'reference/javascript',
  'js/maplibre/README.md': 'reference/maplibre',
};
const siteUrl = 'https://chronozarr.org';
// llms.txt sections in reading order: [heading, [[source, description, optional title], ...]].
// Titles come from each source's first `# ` heading unless given.
const llmsSections = [
  ['Specification', [
    ['spec/CHRONOZARR.md', 'Normative Zarr v3 layout: groups, arrays, attributes, time and band metadata, validity.'],
  ]],
  ['Getting started', [
    ['README.md', 'What chronozarr is, install, the CLI commands and the live demo.', 'chronozarr overview'],
    ['examples/bring_your_data/README.md', 'Encode your own dated rasters into a store and view it.'],
  ]],
  ['Guides', [
    ['docs/hosting.md', 'Publish a store on S3 and CloudFront, R2, GCS or Source Cooperative; CORS and range checks with `chronozarr doctor`.'],
    ['docs/append.md', 'Add new timesteps to an existing store.'],
    ['docs/embedding.md', 'Embed the viewer in a page or notebook.'],
    ['docs/private.md', 'Open a store that needs a login or signed URLs.'],
  ]],
  ['Reference', [
    ['docs/python.md', 'Encode, open_store, convert, validate, export_cog, xarray backend.'],
    ['js/README.md', 'DOM-free JavaScript reader built on zarrita.', 'chronozarr JavaScript reader'],
    ['js/maplibre/README.md', 'MapLibre custom layer that renders a store on the GPU.'],
  ]],
  ['Optional', [
    ['docs/format-comparison.md', 'How the layout compares with other formats.'],
    ['docs/png-frames.md', 'Turn a folder of PNG frames into a store.'],
    ['docs/viewer-distribution.md', 'Self-host the viewer files.'],
  ]],
];
const llmsHeader = `# chronozarr

> chronozarr is a Zarr v3 layout convention for raster time series with true stored values and a multiscale pyramid, readable by xarray and any Zarr client and served from static hosting.

A store is a set of static files in a bucket: no server, no tile service, no auth. The Python package \`chronozarr\` encodes, validates and appends to stores; the JavaScript reader and the browser viewer decode the same files. Each page below is raw Markdown.
`;

// `linkTo(route)` gives the URL of a published page; other repository files link to GitHub.
function rewriteLinks(text, source, linkTo) {
  return text.replace(/\]\(([^)]+)\)/g, (match, href) => {
    if (/^(?:https?:|#|mailto:)/.test(href)) return match;
    const [path, anchor] = href.split('#');
    const target = relative(repo, resolve(repo, dirname(source), path));
    const isDirectory = existsSync(resolve(repo, target)) && statSync(resolve(repo, target)).isDirectory();
    const link = sources[target]
      ? linkTo(sources[target])
      : `https://github.com/chronozarr/chronozarr/${isDirectory ? 'tree' : 'blob'}/main/${target}`;
    return `](${link}${anchor ? `#${anchor}` : ''})`;
  });
}

// The README opens with repository status badges. A docs page does not show them.
function stripBadges(text) {
  return text.replace(/^\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)[ \t]*\n/gm, '');
}

function writeSiteFile(path, text) {
  const output = resolve(repo, path);
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, text);
}

// guides/ and reference/ hold only synced pages (site/.gitignore): clear them, so a page dropped from `sources` stops being published.
for (const directory of ['guides', 'reference']) {
  rmSync(resolve(repo, 'site/docs/pages', directory), { recursive: true, force: true });
  rmSync(resolve(repo, 'site/docs/public', directory), { recursive: true, force: true });
}
const raw = {};
for (const [source, route] of Object.entries(sources)) {
  const original = readFileSync(resolve(repo, source), 'utf8');
  // Raw Markdown for agents: absolute .md links, no MDX escaping.
  raw[source] = stripBadges(rewriteLinks(original, source, target => `${siteUrl}/${target}.md`));
  writeSiteFile(`site/docs/public/${route}.md`, raw[source]);
  let text = stripBadges(rewriteLinks(original, source, target => `/${target}`));
  // Vocs parses Markdown as MDX: protect literal prose braces/angles, while
  // preserving fenced and inline code verbatim. No authored source is changed.
  text = text.replace(/<!--[\s\S]*?-->/g, '');
  text = text.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^`]*`+)/g)
    .map(part => part.startsWith('`') || part.startsWith('~~~') ? part
      : part.replaceAll('<', '&lt;').replaceAll('{', '&#123;').replaceAll('}', '&#125;'))
    .join('');
  writeSiteFile(`site/docs/pages/${route}.md`, text);
}

const listed = llmsSections.flatMap(([, entries]) => entries.map(([source]) => source));
const unlisted = Object.keys(sources).filter(source => !listed.includes(source));
if (unlisted.length || listed.length !== Object.keys(sources).length) {
  throw new Error(`llmsSections must list every key of sources exactly once; unlisted: ${unlisted.join(', ') || 'none'}`);
}
let llms = llmsHeader;
const pages = [];
for (const [heading, entries] of llmsSections) {
  llms += `\n## ${heading}\n\n`;
  if (heading === 'Getting started') {
    llms += `- [Live demo](${siteUrl}/demo): 117 monthly Sentinel-2 composites of the Ucayali River, read from a bucket.\n`;
    llms += '- [Agent skill](https://github.com/chronozarr/chronozarr/blob/main/skills/chronozarr/SKILL.md): Instructions for coding agents that encode, host or read chronozarr stores.\n';
  }
  for (const [source, description, title] of entries) {
    const url = `${siteUrl}/${sources[source]}.md`;
    llms += `- [${title ?? raw[source].match(/^# (.+)$/m)[1]}](${url}): ${description}\n`;
    pages.push({ url, text: raw[source] });
  }
}
writeSiteFile('site/docs/public/llms.txt', llms);
writeSiteFile('site/docs/public/llms-full.txt', pages
  .map(({ url, text }) => `${'='.repeat(72)}\nSource: ${url}\n${'='.repeat(72)}\n\n${text.trim()}\n`)
  .join('\n'));
console.log(`Synced ${Object.keys(sources).length} source documents.`);
