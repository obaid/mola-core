import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

test('window grants bind authenticated XRes process identity and capture/input under one X server grab', () => {
  const result = spawnSync('python3', [fileURLToPath(new URL('./window-identity-fixture.py', import.meta.url)), fileURLToPath(new URL('../runtime/guest_tools.py', import.meta.url))], { encoding: 'utf8', timeout: 25_000 });
  assert.equal(result.status, 0, result.stderr + result.stdout);
});
