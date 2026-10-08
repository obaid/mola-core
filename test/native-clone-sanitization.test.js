import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

test('clone staging removes managed proxy authentication and service persistence', () => {
  const fixture = fileURLToPath(new URL('./native-clone-sanitization-fixture.py', import.meta.url));
  const module = fileURLToPath(new URL('../runtime/native/sanitize_clone.py', import.meta.url));
  const result = spawnSync('python3', [fixture, module], { encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0, result.stderr || result.stdout);
});
