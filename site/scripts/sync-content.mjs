import { existsSync, readFileSync, writeFileSync, mkdirSync, rmSync, statSync } from 'node:fs';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const sources = {
  'README.md': 'getting-started',
  'examples/bring_your_data/README.md': 'guides/bring-your-data',
  'spec/CHRONOZARR.md': 'specification',
  'docs/hosting.md': 'guides/hosting',
  'docs/publish.md': 'guides/publish',
  'docs/hosting-providers.md': 'guides/hosting-providers',
  'docs/hosting-requirements.md': 'guides/hosting-requirements',
  'docs/append.md': 'guides/append',
  'docs/embedding.md': 'guides/embedding',
  'docs/private.md': 'guides/private',
  'docs/viewer-distribution.md': 'guides/viewer-distribution',
  'docs/format-comparison.md': 'guides/format-comparison',
  'docs/png-frames.md': 'guides/png-frames',
  'docs/python.md': 'reference/python',
  'docs/cli.md': 'reference/cli',
  'docs/how-it-works.md': 'how-it-works',
  'docs/share.md': 'guides/share',
  'docs/preview.md': 'guides/preview',
  'docs/notebooks.md': 'guides/notebooks',
  'docs/convert.md': 'guides/convert',
  'js/README.md': 'reference/javascript',
  'js/maplibre/README.md': 'reference/maplibre',
};
// guides/ and reference/ hold only synced pages (site/.gitignore): clear them, so a page dropped from `sources` stops being published.
for (const directory of ['guides', 'reference']) {
  rmSync(resolve(repo, 'site/docs/pages', directory), { recursive: true, force: true });
}
for (const [source, route] of Object.entries(sources)) {
  let text = readFileSync(resolve(repo, source), 'utf8');
  text = text.replace(/\]\(([^)]+)\)/g, (match, href) => {
    if (/^(?:https?:|#|mailto:)/.test(href)) return match;
    const [path, anchor] = href.split('#');
    const target = relative(repo, resolve(repo, dirname(source), path));
    const isDirectory = existsSync(resolve(repo, target)) && statSync(resolve(repo, target)).isDirectory();
    const link = sources[target]
      ? `/${sources[target]}`
      : `https://github.com/chronozarr/chronozarr/${isDirectory ? 'tree' : 'blob'}/main/${target}`;
    return `](${link}${anchor ? `#${anchor}` : ''})`;
  });
  // The README opens with repository status badges. A docs page does not show them.
  text = text.replace(/^\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)[ \t]*\n/gm, '');
  // Vocs parses Markdown as MDX: protect literal prose braces/angles, while
  // preserving fenced and inline code verbatim. No authored source is changed.
  text = text.replace(/<!--[\s\S]*?-->/g, '');
  text = text.split(/(```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^`]*`+)/g)
    .map(part => part.startsWith('`') || part.startsWith('~~~') ? part
      : part.replaceAll('<', '&lt;').replaceAll('{', '&#123;').replaceAll('}', '&#125;'))
    .join('');
  const output = resolve(repo, `site/docs/pages/${route}.md`);
  mkdirSync(dirname(output), { recursive: true });
  writeFileSync(output, text);
}
console.log(`Synced ${Object.keys(sources).length} source documents.`);
