// Fail the build when docs/styles.css targets a Vocs class name the installed Vocs no longer emits.
// Vocs generates its class names from internal component names; a release can rename them without
// notice, and a rule aimed at a missing name silently stops applying.
import { readdir, readFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const site = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const styles = await readFile(resolve(site, 'docs/styles.css'), 'utf8');
const wanted = new Set(styles.match(/vocs_[A-Za-z0-9]+_[A-Za-z0-9_]+/g) ?? []);

const assets = resolve(site, 'docs/dist/assets');
const built = (await readdir(assets)).filter(name => name.endsWith('.css'));
if (built.length === 0) throw new Error(`no built stylesheet in ${assets}; run vocs build first`);
const emitted = new Set();
for (const name of built) {
  for (const match of (await readFile(resolve(assets, name), 'utf8')).match(/vocs_[A-Za-z0-9]+_[A-Za-z0-9_]+/g) ?? []) emitted.add(match);
}

const missing = [...wanted].filter(name => !emitted.has(name)).sort();
if (missing.length > 0) {
  console.error(`docs/styles.css targets ${missing.length} Vocs class name(s) the installed Vocs does not emit:\n  ${missing.join('\n  ')}\nCheck the names in the built CSS and update styles.css.`);
  process.exit(1);
}
console.log(`styles.css: all ${wanted.size} Vocs class names present in the built CSS.`);
