import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

test('opt-in session autosave preserves last good private state across failed probes and crashes', () => {
  const fixture = fileURLToPath(new URL('./session-agent-fixture.py', import.meta.url));
  const module = fileURLToPath(new URL('../runtime/session_agent.py', import.meta.url));
  const result = spawnSync('python3', [fixture, module], { encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.match(result.stderr, /Ran 27 tests/);
});
