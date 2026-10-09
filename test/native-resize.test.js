import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
test('native resize preserves disks across failures and reconciles prepared commits', () => {
  const fixture = fileURLToPath(new URL('./native-resize-fixture.py', import.meta.url));
  const native = fileURLToPath(new URL('../runtime/native/host.py', import.meta.url));
  const result = spawnSync('python3', [fixture, native], { encoding: 'utf8', timeout: 30_000 });
  assert.equal(result.status, 0, result.stderr + result.stdout);
  assert.match(result.stdout, /prepared replay passed/);
});
