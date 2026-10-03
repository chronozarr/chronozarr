import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const sources = {
  'examples/bring_your_data/README.md': 'guides/bring-your-data',
  'examples/bring_your_data/large/README.md': 'guides/larger-source-check',
  'examples/bring_your_data/http_stress/README.md': 'guides/http-stress',
  'bench/adoption/README.md': 'guides/retrieval-comparison',
  'bench/rendered/README.md': 'guides/rendered-comparison',
  'spec/CHRONOZARR.md': 'specification',
  'docs/hosting.md': 'guides/hosting',
  'docs/append.md': 'guides/append',
  'docs/embedding.md': 'guides/embedding',
  'docs/viewer-distribution.md': 'guides/viewer-distribution',
  'docs/format-comparison.md': 'guides/format-comparison',
  'docs/comparisons.md': 'guides/comparisons',
  'docs/png-frames.md': 'guides/png-frames',
  'docs/user-zero.md': 'guides/water-masks',
  'js/README.md': 'reference/javascript',
  'js/maplibre/README.md': 'reference/maplibre',
};
for (const [source, route] of Object.entries(sources)) {
  let text = readFileSync(resolve(repo, source), 'utf8');
  text = text.replace(/\]\(([^)]+)\)/g, (match, href) => {
    if (/^(?:https?:|#|mailto:)/.test(href)) return match;
    const [path, anchor] = href.split('#');
    const target = relative(repo, resolve(repo, dirname(source), path));
    const link = sources[target] ? `/${sources[target]}` : `https://github.com/chronozarr/chronozarr/blob/main/${target}`;
    return `](${link}${anchor ? `#${anchor}` : ''})`;
  });
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
