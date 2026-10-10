import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { test } from 'node:test';
import assert from 'node:assert/strict';

const run = promisify(execFile);
const session = fileURLToPath(new URL('./session.mjs', import.meta.url));

test('withTimeout clears its losing timer so an accepted session does not keep Node alive', async () => {
  const source = `import { withTimeout } from ${JSON.stringify(session)}; await withTimeout(Promise.resolve('done'), 60000, 'test');`;
  const { stdout } = await run(process.execPath, ['--input-type=module', '--eval', source], { timeout: 2000 });
  assert.equal(stdout, '');
});
