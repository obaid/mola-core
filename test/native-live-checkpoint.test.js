import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

test('native checkpoint crash recovery, guards and private guest helper fixture', () => {
  const fixture = fileURLToPath(new URL('./native-live-checkpoint-fixture.py', import.meta.url));
  const module = fileURLToPath(new URL('../runtime/native/live_checkpoint.py', import.meta.url));
  const result = spawnSync('python3', [fixture, module], { encoding: 'utf8', timeout: 30000 });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /Ran 29 tests/);
});
