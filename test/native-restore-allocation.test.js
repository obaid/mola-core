import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

test('restoring older snapshots preserves upgraded allocation through staged growth', () => {
  const fixture = fileURLToPath(new URL('./native-restore-allocation-fixture.py', import.meta.url));
  const native = fileURLToPath(new URL('../runtime/native/host.py', import.meta.url));
  const result = spawnSync('python3', [fixture, native], { encoding: 'utf8', timeout: 30_000 });
  assert.equal(result.status, 0, result.stderr + result.stdout);
  assert.match(result.stdout, /failure preservation and prepared replay passed/);
});
