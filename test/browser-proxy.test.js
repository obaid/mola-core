import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { validateBrowserProxy } from '../src/browser-proxy.js';

test('proxy bridge forwards HTTP and opaque HTTPS without authentication prompts or direct fallback', () => {
  const fixture = fileURLToPath(new URL('./browser-proxy-fixture.py', import.meta.url));
  const module = fileURLToPath(new URL('../runtime/browser_proxy.py', import.meta.url));
  const result = spawnSync('python3', [fixture, module], { encoding: 'utf8', timeout: 10000 });
  assert.equal(result.status, 0, result.stderr || result.stdout || result.error?.message);
});

test('private proxy contract rejects credential injection and nested unknown fields', () => {
  const proxy = { provider: 'decodo', host: 'isp.decodo.com', port: 10001, username: 'fixture', password: 'fixture' };
  assert.deepEqual(validateBrowserProxy(proxy, 'ubuntu-xfce:24.04-4'), proxy);
  for (const patch of [{ password: 'fixture\r\nInjected: yes' }, { username: 'user:secret' }, { port: '7000' }, { bypass: 'DIRECT' }]) {
    assert.throws(() => validateBrowserProxy({ ...proxy, ...patch }, 'ubuntu-xfce:24.04-4'), /Invalid/);
  }
});
